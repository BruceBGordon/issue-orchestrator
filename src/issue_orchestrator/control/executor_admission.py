"""Pure learning and admission policy for the machine-wide executor."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)
from ..domain.executor_monitoring import ExecutorRequestId, ExecutorWaitReason
from ..domain.executor_host import ExecutorHostCpuUtilization


def _require_exact_type(
    owner: str,
    field: str,
    value: object,
    expected: type,
) -> None:
    if type(value) is not expected:
        raise ValueError(f"{owner}.{field} must have exact type {expected.__name__}")


def _require_positive_integer(owner: str, field: str, value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{owner}.{field} must be a positive integer")


def _require_non_negative_integer(owner: str, field: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{owner}.{field} must be a non-negative integer")


def _require_finite_positive(owner: str, field: str, value: float) -> None:
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{owner}.{field} must be finite and positive")


def _require_finite_non_negative(owner: str, field: str, value: float) -> None:
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise ValueError(f"{owner}.{field} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ExecutorResourceObservation:
    """Internal resource measurement used to learn admission pressure."""

    concurrency: int
    wall_seconds: float
    cpu_seconds: float
    executor_process_lifetime_children_max_rss_bytes: int
    input_blocks: int
    output_blocks: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_positive_integer(owner, "concurrency", self.concurrency)
        _require_finite_positive(owner, "wall_seconds", self.wall_seconds)
        _require_finite_non_negative(owner, "cpu_seconds", self.cpu_seconds)
        for field_name, value in (
            (
                "executor_process_lifetime_children_max_rss_bytes",
                self.executor_process_lifetime_children_max_rss_bytes,
            ),
            ("input_blocks", self.input_blocks),
            ("output_blocks", self.output_blocks),
        ):
            _require_non_negative_integer(owner, field_name, value)

    @property
    def cores_per_concurrency(self) -> float:
        """Average occupied cores per granted concurrency unit."""
        return self.cpu_seconds / self.wall_seconds / self.concurrency


@dataclass(frozen=True, slots=True)
class ExecutorAdmissionGrant:
    """Internal concurrency choice and CPU-slot charge for one admission."""

    concurrency: int
    cpu_slots: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_positive_integer(owner, "concurrency", self.concurrency)
        _require_positive_integer(owner, "cpu_slots", self.cpu_slots)


@dataclass(frozen=True, slots=True)
class ExecutorLearnedDemand:
    """Estimated occupied cores per concurrency unit."""

    cores_per_concurrency: float

    def __post_init__(self) -> None:
        _require_finite_positive(
            type(self).__name__,
            "cores_per_concurrency",
            self.cores_per_concurrency,
        )


@dataclass(frozen=True, slots=True)
class ExecutorLearningPolicy:
    """Explicit cold-start and smoothing policy for work observations."""

    cold_start_cores_per_concurrency: float
    minimum_cores_per_concurrency: float
    recent_observation_weight: float

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_finite_positive(
            owner,
            "cold_start_cores_per_concurrency",
            self.cold_start_cores_per_concurrency,
        )
        _require_finite_positive(
            owner,
            "minimum_cores_per_concurrency",
            self.minimum_cores_per_concurrency,
        )
        if (
            type(self.recent_observation_weight) is not float
            or not math.isfinite(self.recent_observation_weight)
            or not 0 < self.recent_observation_weight <= 1
        ):
            raise ValueError(
                "ExecutorLearningPolicy.recent_observation_weight must be in (0, 1]"
            )


@dataclass(frozen=True, slots=True)
class ExecutorSaturationPolicy:
    """Internal threshold at which new admissions pause for host recovery."""

    maximum_busy_percent: int

    def __post_init__(self) -> None:
        if (
            type(self.maximum_busy_percent) is not int
            or not 1 <= self.maximum_busy_percent <= 100
        ):
            raise ValueError(
                "ExecutorSaturationPolicy.maximum_busy_percent must be in [1, 100]"
            )

    def requires_attenuation(
        self,
        utilization: ExecutorHostCpuUtilization,
    ) -> bool:
        _require_exact_type(
            type(self).__name__,
            "utilization",
            utilization,
            ExecutorHostCpuUtilization,
        )
        return utilization.busy_percent >= self.maximum_busy_percent


class ExecutorWorkDemandEstimator:
    """Convert ordered observations into a stable learned demand."""

    def __init__(self, policy: ExecutorLearningPolicy) -> None:
        _require_exact_type(
            type(self).__name__, "policy", policy, ExecutorLearningPolicy
        )
        self._policy = policy

    def estimate(
        self,
        observations: tuple[ExecutorResourceObservation, ...],
    ) -> ExecutorLearnedDemand:
        """Apply an exponentially weighted estimate in observation order."""
        _require_exact_type(type(self).__name__, "observations", observations, tuple)
        if any(
            type(observation) is not ExecutorResourceObservation
            for observation in observations
        ):
            raise ValueError(
                "ExecutorWorkDemandEstimator.observations must contain only "
                "ExecutorResourceObservation values"
            )
        estimate = self._policy.cold_start_cores_per_concurrency
        recent_weight = self._policy.recent_observation_weight
        historical_weight = 1 - recent_weight
        for observation in observations:
            observed = max(
                self._policy.minimum_cores_per_concurrency,
                observation.cores_per_concurrency,
            )
            estimate = historical_weight * estimate + recent_weight * observed
        return ExecutorLearnedDemand(
            max(self._policy.minimum_cores_per_concurrency, estimate)
        )


@dataclass(frozen=True, slots=True)
class QueuedExecutorWork:
    """Complete scheduling facts for one live queued request."""

    request_id: ExecutorRequestId
    sequence: int
    work_key: ExecutorWorkKey
    fairness_group: ExecutorFairnessGroup
    concurrency_range: ExecutorConcurrencyRange
    learned_demand: ExecutorLearnedDemand
    aggressiveness: ExecutorAggressiveness
    exclusive_resources: tuple[ExecutorExclusiveResource, ...]

    def __post_init__(self) -> None:
        owner = type(self).__name__
        for field_name, value, expected_type in (
            ("request_id", self.request_id, ExecutorRequestId),
            ("work_key", self.work_key, ExecutorWorkKey),
            ("fairness_group", self.fairness_group, ExecutorFairnessGroup),
            (
                "concurrency_range",
                self.concurrency_range,
                ExecutorConcurrencyRange,
            ),
            ("learned_demand", self.learned_demand, ExecutorLearnedDemand),
            ("aggressiveness", self.aggressiveness, ExecutorAggressiveness),
        ):
            _require_exact_type(owner, field_name, value, expected_type)
        _require_positive_integer(owner, "sequence", self.sequence)
        _require_exact_type(
            owner,
            "exclusive_resources",
            self.exclusive_resources,
            tuple,
        )
        if any(
            type(resource) is not ExecutorExclusiveResource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "QueuedExecutorWork.exclusive_resources must contain only "
                "ExecutorExclusiveResource values"
            )
        resources = tuple(resource.value for resource in self.exclusive_resources)
        if len(resources) != len(set(resources)):
            raise ValueError(
                "QueuedExecutorWork.exclusive_resources must not contain duplicates"
            )


@dataclass(frozen=True, slots=True)
class ActiveExecutorLease:
    """Scheduling facts for one live admitted command."""

    fairness_group: ExecutorFairnessGroup
    grant: ExecutorAdmissionGrant
    exclusive_resources: tuple[ExecutorExclusiveResource, ...]

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact_type(
            owner,
            "fairness_group",
            self.fairness_group,
            ExecutorFairnessGroup,
        )
        _require_exact_type(owner, "grant", self.grant, ExecutorAdmissionGrant)
        _require_exact_type(
            owner,
            "exclusive_resources",
            self.exclusive_resources,
            tuple,
        )
        if any(
            type(resource) is not ExecutorExclusiveResource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "ActiveExecutorLease.exclusive_resources must contain only "
                "ExecutorExclusiveResource values"
            )
        resources = tuple(resource.value for resource in self.exclusive_resources)
        if len(resources) != len(set(resources)):
            raise ValueError(
                "ActiveExecutorLease.exclusive_resources must not contain duplicates"
            )


@dataclass(frozen=True, slots=True)
class ExecutorGroupService:
    """CPU slots already admitted for one live fairness group."""

    fairness_group: ExecutorFairnessGroup
    cpu_slots: int

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__,
            "fairness_group",
            self.fairness_group,
            ExecutorFairnessGroup,
        )
        _require_non_negative_integer(type(self).__name__, "cpu_slots", self.cpu_slots)


@dataclass(frozen=True, slots=True)
class ExecutorQueueSnapshot:
    """Atomic facts used for one admission decision."""

    host_cpu_slots: int
    queued: tuple[QueuedExecutorWork, ...]
    active: tuple[ActiveExecutorLease, ...]
    group_service: tuple[ExecutorGroupService, ...]
    host_cpu_utilization: ExecutorHostCpuUtilization

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_positive_integer(owner, "host_cpu_slots", self.host_cpu_slots)
        _require_exact_type(
            owner,
            "host_cpu_utilization",
            self.host_cpu_utilization,
            ExecutorHostCpuUtilization,
        )
        for field_name, values, expected_type in (
            ("queued", self.queued, QueuedExecutorWork),
            ("active", self.active, ActiveExecutorLease),
            ("group_service", self.group_service, ExecutorGroupService),
        ):
            _require_exact_type(owner, field_name, values, tuple)
            if any(type(value) is not expected_type for value in values):
                raise ValueError(
                    f"ExecutorQueueSnapshot.{field_name} must contain only "
                    f"{expected_type.__name__} values"
                )
        request_ids = tuple(request.request_id for request in self.queued)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("ExecutorQueueSnapshot.queued request IDs must be unique")
        service_groups = tuple(item.fairness_group for item in self.group_service)
        if len(service_groups) != len(set(service_groups)):
            raise ValueError(
                "ExecutorQueueSnapshot.group_service groups must be unique"
            )
        live_groups = {
            *(request.fairness_group for request in self.queued),
            *(lease.fairness_group for lease in self.active),
        }
        if set(service_groups) != live_groups:
            raise ValueError(
                "ExecutorQueueSnapshot.group_service must contain every live group "
                "exactly once"
            )


@dataclass(frozen=True, slots=True)
class ExecutorAdmissionGranted:
    """Admission selected from one atomic snapshot."""

    grant: ExecutorAdmissionGrant
    leased_cpu_slots_before: int
    available_cpu_slots_before: int
    reserved_cpu_slots_for_queued_peers: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact_type(owner, "grant", self.grant, ExecutorAdmissionGrant)
        _require_non_negative_integer(
            owner,
            "leased_cpu_slots_before",
            self.leased_cpu_slots_before,
        )
        _require_non_negative_integer(
            owner,
            "available_cpu_slots_before",
            self.available_cpu_slots_before,
        )
        _require_non_negative_integer(
            owner,
            "reserved_cpu_slots_for_queued_peers",
            self.reserved_cpu_slots_for_queued_peers,
        )
        if (
            self.grant.cpu_slots + self.reserved_cpu_slots_for_queued_peers
            > self.available_cpu_slots_before
        ):
            raise ValueError(
                "ExecutorAdmissionGranted grant plus peer reservation must not "
                "exceed available CPU slots"
            )


@dataclass(frozen=True, slots=True)
class ExecutorAdmissionDeferred:
    """Deferral selected from one atomic snapshot."""

    reason: ExecutorWaitReason
    leased_cpu_slots: int
    available_cpu_slots: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact_type(owner, "reason", self.reason, ExecutorWaitReason)
        _require_non_negative_integer(
            owner,
            "leased_cpu_slots",
            self.leased_cpu_slots,
        )
        _require_non_negative_integer(
            owner,
            "available_cpu_slots",
            self.available_cpu_slots,
        )


ExecutorAdmissionDecision = ExecutorAdmissionGranted | ExecutorAdmissionDeferred


class ExecutorAdmissionPolicy:
    """Select fair, resource-safe grants without performing external I/O."""

    def __init__(self, saturation: ExecutorSaturationPolicy) -> None:
        _require_exact_type(
            type(self).__name__,
            "saturation",
            saturation,
            ExecutorSaturationPolicy,
        )
        self._saturation = saturation

    def decide(
        self,
        current: QueuedExecutorWork,
        snapshot: ExecutorQueueSnapshot,
    ) -> ExecutorAdmissionDecision:
        """Return the grant or exact deferral reason for ``current``."""
        _require_exact_type(type(self).__name__, "current", current, QueuedExecutorWork)
        _require_exact_type(
            type(self).__name__, "snapshot", snapshot, ExecutorQueueSnapshot
        )
        matching_current = tuple(
            request
            for request in snapshot.queued
            if request.request_id == current.request_id
        )
        if matching_current != (current,):
            raise ValueError(
                "current request must appear exactly once in ExecutorQueueSnapshot"
            )

        leased = sum(lease.grant.cpu_slots for lease in snapshot.active)
        if leased > snapshot.host_cpu_slots:
            raise ValueError("active executor leases exceed host capacity")
        available = snapshot.host_cpu_slots - leased
        if self._saturation.requires_attenuation(snapshot.host_cpu_utilization):
            return ExecutorAdmissionDeferred(
                ExecutorWaitReason.HOST_PRESSURE,
                leased,
                available,
            )
        busy_resources = {
            resource
            for lease in snapshot.active
            for resource in lease.exclusive_resources
        }
        if busy_resources.intersection(current.exclusive_resources):
            return ExecutorAdmissionDeferred(
                ExecutorWaitReason.EXCLUSIVE_RESOURCE,
                leased,
                available,
            )

        resource_eligible = tuple(
            request
            for request in snapshot.queued
            if busy_resources.isdisjoint(request.exclusive_resources)
        )
        selected_group_head = self._select_fair_request(
            resource_eligible,
            snapshot.group_service,
        )
        if selected_group_head.fairness_group != current.fairness_group:
            return ExecutorAdmissionDeferred(
                ExecutorWaitReason.FAIRNESS,
                leased,
                available,
            )
        if selected_group_head.request_id != current.request_id:
            return ExecutorAdmissionDeferred(
                ExecutorWaitReason.FAIRNESS,
                leased,
                available,
            )

        minimum_grant = self._smallest_grant(current, snapshot.host_cpu_slots)
        if minimum_grant.cpu_slots > available:
            return ExecutorAdmissionDeferred(
                ExecutorWaitReason.CAPACITY,
                leased,
                available,
            )
        compatible_peers = self._select_compatible_peer_cohort(
            current,
            resource_eligible,
            snapshot.group_service,
        )
        peer_reservation = sum(
            self._smallest_grant(peer, snapshot.host_cpu_slots).cpu_slots
            for peer in compatible_peers
        )
        grant_budget = max(
            minimum_grant.cpu_slots,
            available - peer_reservation,
        )
        grant = self._largest_grant(
            current,
            snapshot.host_cpu_slots,
            grant_budget,
        )
        if grant is None:
            raise AssertionError("the current minimum grant must fit its budget")
        return ExecutorAdmissionGranted(
            grant,
            leased,
            available,
            min(peer_reservation, available - minimum_grant.cpu_slots),
        )

    @staticmethod
    def _select_compatible_peer_cohort(
        current: QueuedExecutorWork,
        eligible: tuple[QueuedExecutorWork, ...],
        service: tuple[ExecutorGroupService, ...],
    ) -> tuple[QueuedExecutorWork, ...]:
        """Choose the deterministic peer set that could coexist with current."""
        service_by_group = {item.fairness_group: item.cpu_slots for item in service}
        ordered_peers = sorted(
            (peer for peer in eligible if peer.request_id != current.request_id),
            key=lambda peer: (
                service_by_group[peer.fairness_group],
                peer.sequence,
                peer.request_id.value,
            ),
        )
        reserved_resources = set(current.exclusive_resources)
        compatible: list[QueuedExecutorWork] = []
        for peer in ordered_peers:
            peer_resources = set(peer.exclusive_resources)
            if not reserved_resources.isdisjoint(peer_resources):
                continue
            compatible.append(peer)
            reserved_resources.update(peer_resources)
        return tuple(compatible)

    @staticmethod
    def _select_fair_request(
        eligible: tuple[QueuedExecutorWork, ...],
        service: tuple[ExecutorGroupService, ...],
    ) -> QueuedExecutorWork:
        if not eligible:
            raise ValueError("at least one queued request must be resource-eligible")
        service_by_group = {item.fairness_group: item.cpu_slots for item in service}
        oldest_by_group: dict[ExecutorFairnessGroup, QueuedExecutorWork] = {}
        for request in eligible:
            previous = oldest_by_group.get(request.fairness_group)
            if previous is None or (
                request.sequence,
                request.request_id.value,
            ) < (
                previous.sequence,
                previous.request_id.value,
            ):
                oldest_by_group[request.fairness_group] = request
        return min(
            oldest_by_group.values(),
            key=lambda request: (
                service_by_group[request.fairness_group],
                request.sequence,
                request.request_id.value,
            ),
        )

    def _largest_grant(
        self,
        request: QueuedExecutorWork,
        host_cpu_slots: int,
        available_cpu_slots: int,
    ) -> ExecutorAdmissionGrant | None:
        minimum_grant = self._smallest_grant(request, host_cpu_slots)
        for concurrency in range(
            request.concurrency_range.maximum_concurrency,
            request.concurrency_range.minimum_concurrency,
            -1,
        ):
            estimated = math.ceil(
                request.learned_demand.cores_per_concurrency * concurrency
            )
            charged = self._scaled_capacity(
                estimated,
                request.aggressiveness,
            )
            if charged <= available_cpu_slots:
                return ExecutorAdmissionGrant(concurrency, charged)
        if minimum_grant.cpu_slots <= available_cpu_slots:
            return minimum_grant
        return None

    def _smallest_grant(
        self,
        request: QueuedExecutorWork,
        host_cpu_slots: int,
    ) -> ExecutorAdmissionGrant:
        concurrency = request.concurrency_range.minimum_concurrency
        estimated = math.ceil(
            request.learned_demand.cores_per_concurrency * concurrency
        )
        return ExecutorAdmissionGrant(
            concurrency,
            min(
                host_cpu_slots,
                self._scaled_capacity(
                    estimated,
                    request.aggressiveness,
                ),
            ),
        )

    @staticmethod
    def _scaled_capacity(
        estimated_cpu_slots: int,
        aggressiveness: ExecutorAggressiveness,
    ) -> int:
        scaled = math.ceil(estimated_cpu_slots * 100 / aggressiveness.percent)
        return max(1, scaled)
