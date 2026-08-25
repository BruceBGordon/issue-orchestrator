"""Virtual-time DSL for adaptive executor workload simulations."""

from __future__ import annotations

import math
from dataclasses import dataclass

from issue_orchestrator.control.executor_admission import (
    ActiveExecutorLease,
    ExecutorAdmissionGrant,
    ExecutorAdmissionGranted,
    ExecutorAdmissionPolicy,
    ExecutorGroupService,
    ExecutorLearnedDemand,
    ExecutorLearningPolicy,
    ExecutorQueueSnapshot,
    ExecutorSaturationPolicy,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_host import ExecutorHostCpuUtilization
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorRequestId,
    ExecutorWaitReason,
)


def _require_finite_non_negative(owner: str, field: str, value: float) -> None:
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise ValueError(f"{owner}.{field} must be finite and non-negative")


def _require_finite_positive(owner: str, field: str, value: float) -> None:
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{owner}.{field} must be finite and positive")


@dataclass(frozen=True, slots=True)
class SimulatedLaneProfile:
    """Controllable behavior of one opaque lane in every validation microburst."""

    work_key: ExecutorWorkKey
    concurrency_range: ExecutorConcurrencyRange
    learned_cores_per_concurrency: float
    actual_cores_per_concurrency: float
    serial_seconds: float
    parallel_seconds_at_one_concurrency: float
    exclusive_resources: tuple[ExecutorExclusiveResource, ...]

    def __post_init__(self) -> None:
        owner = type(self).__name__
        if type(self.work_key) is not ExecutorWorkKey:
            raise ValueError("SimulatedLaneProfile.work_key must be ExecutorWorkKey")
        if type(self.concurrency_range) is not ExecutorConcurrencyRange:
            raise ValueError(
                "SimulatedLaneProfile.concurrency_range must be "
                "ExecutorConcurrencyRange"
            )
        _require_finite_positive(
            owner,
            "learned_cores_per_concurrency",
            self.learned_cores_per_concurrency,
        )
        _require_finite_positive(
            owner,
            "actual_cores_per_concurrency",
            self.actual_cores_per_concurrency,
        )
        _require_finite_non_negative(owner, "serial_seconds", self.serial_seconds)
        _require_finite_non_negative(
            owner,
            "parallel_seconds_at_one_concurrency",
            self.parallel_seconds_at_one_concurrency,
        )
        if self.serial_seconds + self.parallel_seconds_at_one_concurrency <= 0:
            raise ValueError("SimulatedLaneProfile duration must be positive")
        if type(self.exclusive_resources) is not tuple or any(
            type(resource) is not ExecutorExclusiveResource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "SimulatedLaneProfile.exclusive_resources must contain only "
                "ExecutorExclusiveResource values"
            )
        resource_names = tuple(
            resource.value for resource in self.exclusive_resources
        )
        if len(resource_names) != len(set(resource_names)):
            raise ValueError(
                "SimulatedLaneProfile.exclusive_resources must not contain duplicates"
            )

    def duration_seconds(self, grant: ExecutorAdmissionGrant) -> float:
        if type(grant) is not ExecutorAdmissionGrant:
            raise ValueError(
                "SimulatedLaneProfile.duration_seconds requires ExecutorAdmissionGrant"
            )
        return self.serial_seconds + (
            self.parallel_seconds_at_one_concurrency / grant.concurrency
        )

    def requested_cpu_cores(self, grant: ExecutorAdmissionGrant) -> float:
        if type(grant) is not ExecutorAdmissionGrant:
            raise ValueError(
                "SimulatedLaneProfile.requested_cpu_cores requires "
                "ExecutorAdmissionGrant"
            )
        return self.actual_cores_per_concurrency * grant.concurrency


@dataclass(frozen=True, slots=True)
class SimulatedArrivalSchedule:
    """Virtual seconds at which validation lane microbursts enter the host."""

    validation_seconds: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.validation_seconds) is not tuple or not self.validation_seconds:
            raise ValueError(
                "SimulatedArrivalSchedule.validation_seconds must be a non-empty tuple"
            )
        for value in self.validation_seconds:
            _require_finite_non_negative(
                type(self).__name__,
                "validation_seconds item",
                value,
            )
        if tuple(sorted(self.validation_seconds)) != self.validation_seconds:
            raise ValueError(
                "SimulatedArrivalSchedule.validation_seconds must be sorted"
            )
        if len(set(self.validation_seconds)) != len(self.validation_seconds):
            raise ValueError(
                "SimulatedArrivalSchedule.validation_seconds must be unique"
            )

    @classmethod
    def evenly_spaced(
        cls,
        *,
        validation_count: int,
        horizon_seconds: float,
    ) -> SimulatedArrivalSchedule:
        if type(validation_count) is not int or validation_count < 1:
            raise ValueError("validation_count must be positive")
        _require_finite_positive(cls.__name__, "horizon_seconds", horizon_seconds)
        if validation_count == 1:
            return cls((0.0,))
        interval = horizon_seconds / (validation_count - 1)
        return cls(
            tuple(float(index * interval) for index in range(validation_count))
        )


@dataclass(frozen=True, slots=True)
class SimulatedExternalCpuWindow:
    """Unmanaged host CPU demand during one virtual-time interval."""

    starts_at_seconds: float
    ends_at_seconds: float
    requested_cpu_cores: float

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_finite_non_negative(
            owner,
            "starts_at_seconds",
            self.starts_at_seconds,
        )
        _require_finite_positive(owner, "ends_at_seconds", self.ends_at_seconds)
        if self.ends_at_seconds <= self.starts_at_seconds:
            raise ValueError(
                "SimulatedExternalCpuWindow.ends_at_seconds must follow its start"
            )
        _require_finite_non_negative(
            owner,
            "requested_cpu_cores",
            self.requested_cpu_cores,
        )

    def demand_at(self, now_seconds: float) -> float:
        _require_finite_non_negative(
            type(self).__name__,
            "now_seconds",
            now_seconds,
        )
        if self.starts_at_seconds <= now_seconds < self.ends_at_seconds:
            return self.requested_cpu_cores
        return 0.0


@dataclass(frozen=True, slots=True)
class SimulatedRunToCompletion:
    """Opaque commands expose no safe pause boundary before completion."""


@dataclass(frozen=True, slots=True)
class SimulatedCooperativeBoundaries:
    """Application-declared safe boundaries measured in active work seconds."""

    active_second_offsets: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.active_second_offsets) is not tuple:
            raise ValueError(
                "SimulatedCooperativeBoundaries.active_second_offsets must be a tuple"
            )
        for offset in self.active_second_offsets:
            _require_finite_positive(type(self).__name__, "offset", offset)
        if tuple(sorted(self.active_second_offsets)) != self.active_second_offsets:
            raise ValueError(
                "SimulatedCooperativeBoundaries.active_second_offsets must be sorted"
            )
        if len(set(self.active_second_offsets)) != len(
            self.active_second_offsets
        ):
            raise ValueError(
                "SimulatedCooperativeBoundaries.active_second_offsets must be unique"
            )

    def intervals(self) -> tuple[float, ...]:
        previous = 0.0
        intervals: list[float] = []
        for offset in self.active_second_offsets:
            intervals.append(offset - previous)
            previous = offset
        return tuple(intervals)


SimulatedExecutionMode = SimulatedRunToCompletion | SimulatedCooperativeBoundaries


@dataclass(frozen=True, slots=True)
class ExecutorSimulationDials:
    """All machine/controller controls for one repeatable virtual run."""

    host_cpu_slots: int
    aggressiveness: ExecutorAggressiveness
    saturation: ExecutorSaturationPolicy
    learning: ExecutorLearningPolicy
    execution_mode: SimulatedExecutionMode
    decision_interval_seconds: float
    maximum_simulation_seconds: float

    def __post_init__(self) -> None:
        if type(self.host_cpu_slots) is not int or self.host_cpu_slots < 1:
            raise ValueError("ExecutorSimulationDials.host_cpu_slots must be positive")
        if type(self.aggressiveness) is not ExecutorAggressiveness:
            raise ValueError(
                "ExecutorSimulationDials.aggressiveness must be "
                "ExecutorAggressiveness"
            )
        if type(self.saturation) is not ExecutorSaturationPolicy:
            raise ValueError(
                "ExecutorSimulationDials.saturation must be ExecutorSaturationPolicy"
            )
        if type(self.learning) is not ExecutorLearningPolicy:
            raise ValueError(
                "ExecutorSimulationDials.learning must be ExecutorLearningPolicy"
            )
        if type(self.execution_mode) not in (
            SimulatedRunToCompletion,
            SimulatedCooperativeBoundaries,
        ):
            raise ValueError(
                "ExecutorSimulationDials.execution_mode must be a supported "
                "SimulatedExecutionMode"
            )
        _require_finite_positive(
            type(self).__name__,
            "decision_interval_seconds",
            self.decision_interval_seconds,
        )
        _require_finite_positive(
            type(self).__name__,
            "maximum_simulation_seconds",
            self.maximum_simulation_seconds,
        )


@dataclass(frozen=True, slots=True)
class ExecutorSimulationScenario:
    """A parameterized sequence of validation microbursts and outside load."""

    dials: ExecutorSimulationDials
    arrivals: SimulatedArrivalSchedule
    lanes: tuple[SimulatedLaneProfile, ...]
    external_cpu_windows: tuple[SimulatedExternalCpuWindow, ...]

    def __post_init__(self) -> None:
        if type(self.dials) is not ExecutorSimulationDials:
            raise ValueError(
                "ExecutorSimulationScenario.dials must be ExecutorSimulationDials"
            )
        if type(self.arrivals) is not SimulatedArrivalSchedule:
            raise ValueError(
                "ExecutorSimulationScenario.arrivals must be "
                "SimulatedArrivalSchedule"
            )
        if type(self.lanes) is not tuple or not self.lanes or any(
            type(lane) is not SimulatedLaneProfile for lane in self.lanes
        ):
            raise ValueError(
                "ExecutorSimulationScenario.lanes must contain "
                "SimulatedLaneProfile values"
            )
        work_keys = tuple(lane.work_key for lane in self.lanes)
        if len(work_keys) != len(set(work_keys)):
            raise ValueError("ExecutorSimulationScenario lane work keys must be unique")
        if type(self.external_cpu_windows) is not tuple or any(
            type(window) is not SimulatedExternalCpuWindow
            for window in self.external_cpu_windows
        ):
            raise ValueError(
                "ExecutorSimulationScenario.external_cpu_windows must contain "
                "SimulatedExternalCpuWindow values"
            )
        if self.arrivals.validation_seconds[-1] > self.dials.maximum_simulation_seconds:
            raise ValueError("last validation arrival exceeds the simulation horizon")


@dataclass(frozen=True, slots=True)
class SimulatedAdmission:
    request_id: ExecutorRequestId
    work_key: ExecutorWorkKey
    fairness_group: ExecutorFairnessGroup
    arrived_at_seconds: float
    admitted_at_seconds: float
    completes_at_seconds: float
    grant: ExecutorAdmissionGrant

    @property
    def queue_seconds(self) -> float:
        return self.admitted_at_seconds - self.arrived_at_seconds


@dataclass(frozen=True, slots=True)
class SimulatedWaitTransition:
    request_id: ExecutorRequestId
    at_seconds: float
    reason: ExecutorWaitReason
    host_cpu_busy_percent: float


@dataclass(frozen=True, slots=True)
class ExecutorSimulationResult:
    admissions: tuple[SimulatedAdmission, ...]
    wait_transitions: tuple[SimulatedWaitTransition, ...]
    completed_request_count: int
    completed_validation_count: int
    finished_at_seconds: float
    maximum_requested_cpu_cores: float
    maximum_charged_cpu_slots: int
    requested_cpu_core_seconds_above_capacity: float
    cooperative_yield_count: int
    learned_demands: tuple[SimulatedLearnedDemand, ...]

    @property
    def maximum_queue_seconds(self) -> float:
        return max((admission.queue_seconds for admission in self.admissions), default=0.0)


@dataclass(frozen=True, slots=True)
class _QueuedSimulationWork:
    work: QueuedExecutorWork
    profile: SimulatedLaneProfile
    arrived_at_seconds: float
    remaining_serial_seconds: float
    remaining_parallel_seconds_at_one_concurrency: float
    resume_count: int
    safe_boundary_intervals_seconds: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SimulatedLearnedDemand:
    work_key: ExecutorWorkKey
    cores_per_concurrency: float


@dataclass(frozen=True, slots=True)
class _ActiveSimulationWork:
    queued: _QueuedSimulationWork
    grant: ExecutorAdmissionGrant
    segment_started_at_seconds: float
    completes_at_seconds: float
    next_safe_boundary_at_seconds: float

    def lease(self) -> ActiveExecutorLease:
        return ActiveExecutorLease(
            fairness_group=self.queued.work.fairness_group,
            grant=self.grant,
            exclusive_resources=self.queued.work.exclusive_resources,
        )


class ExecutorWorkloadSimulator:
    """Drive the real pure admission policy through virtual time."""

    def run(self, scenario: ExecutorSimulationScenario) -> ExecutorSimulationResult:
        if type(scenario) is not ExecutorSimulationScenario:
            raise ValueError(
                "ExecutorWorkloadSimulator.run requires ExecutorSimulationScenario"
            )
        policy = ExecutorAdmissionPolicy(scenario.dials.saturation)
        queued: list[_QueuedSimulationWork] = []
        active: list[_ActiveSimulationWork] = []
        group_service: dict[ExecutorFairnessGroup, int] = {}
        last_wait_reason: dict[ExecutorRequestId, ExecutorWaitReason] = {}
        admissions: list[SimulatedAdmission] = []
        wait_transitions: list[SimulatedWaitTransition] = []
        completed_request_count = 0
        completed_groups: set[ExecutorFairnessGroup] = set()
        maximum_requested_cpu_cores = 0.0
        maximum_charged_cpu_slots = 0
        requested_cpu_core_seconds_above_capacity = 0.0
        cooperative_yield_count = 0
        learned_by_work = {
            profile.work_key: profile.learned_cores_per_concurrency
            for profile in scenario.lanes
        }
        next_arrival = 0
        next_sequence = 1
        now_seconds = 0.0
        total_requests = len(scenario.arrivals.validation_seconds) * len(
            scenario.lanes
        )

        while completed_request_count < total_requests:
            if now_seconds > scenario.dials.maximum_simulation_seconds:
                raise RuntimeError(
                    "executor simulation exceeded maximum_simulation_seconds: "
                    f"completed={completed_request_count}/{total_requests} "
                    f"queued={len(queued)} active={len(active)}"
                )
            finished = tuple(
                item for item in active if item.completes_at_seconds <= now_seconds
            )
            if finished:
                active = [item for item in active if item not in finished]
                completed_request_count += len(finished)
                for item in finished:
                    profile = item.queued.profile
                    previous_learned = learned_by_work[profile.work_key]
                    observed = max(
                        scenario.dials.learning.minimum_cores_per_concurrency,
                        profile.actual_cores_per_concurrency,
                    )
                    recent_weight = scenario.dials.learning.recent_observation_weight
                    learned_by_work[profile.work_key] = max(
                        scenario.dials.learning.minimum_cores_per_concurrency,
                        (1 - recent_weight) * previous_learned
                        + recent_weight * observed,
                    )
                    group = item.queued.work.fairness_group
                    if not any(
                        pending.work.fairness_group == group for pending in queued
                    ) and not any(
                        running.queued.work.fairness_group == group
                        for running in active
                    ):
                        completed_groups.add(group)

            while (
                next_arrival < len(scenario.arrivals.validation_seconds)
                and scenario.arrivals.validation_seconds[next_arrival] <= now_seconds
            ):
                arrived_at = scenario.arrivals.validation_seconds[next_arrival]
                fairness_group = ExecutorFairnessGroup(
                    f"simulated-validation-{next_arrival + 1}"
                )
                for profile in scenario.lanes:
                    request_id = ExecutorRequestId(
                        f"simulated-request-{next_sequence}"
                    )
                    queued.append(
                        _QueuedSimulationWork(
                            work=QueuedExecutorWork(
                                request_id=request_id,
                                sequence=next_sequence,
                                work_key=profile.work_key,
                                fairness_group=fairness_group,
                                concurrency_range=profile.concurrency_range,
                                learned_demand=ExecutorLearnedDemand(
                                    learned_by_work[profile.work_key]
                                ),
                                aggressiveness=scenario.dials.aggressiveness,
                                exclusive_resources=profile.exclusive_resources,
                            ),
                            profile=profile,
                            arrived_at_seconds=arrived_at,
                            remaining_serial_seconds=profile.serial_seconds,
                            remaining_parallel_seconds_at_one_concurrency=(
                                profile.parallel_seconds_at_one_concurrency
                            ),
                            resume_count=0,
                            safe_boundary_intervals_seconds=(
                                ()
                                if isinstance(
                                    scenario.dials.execution_mode,
                                    SimulatedRunToCompletion,
                                )
                                else scenario.dials.execution_mode.intervals()
                            ),
                        )
                    )
                    next_sequence += 1
                next_arrival += 1

            live_groups = {
                *(pending.work.fairness_group for pending in queued),
                *(running.queued.work.fairness_group for running in active),
            }
            group_service = {
                group: group_service.get(group, 0) for group in live_groups
            }
            host_busy_percent = self._host_busy_percent(
                scenario,
                active,
                now_seconds,
            )
            cooperative_yield_count += self._yield_pressured_work(
                scenario,
                now_seconds,
                queued,
                active,
            )
            utilization = ExecutorHostCpuUtilization(
                host_busy_percent,
                scenario.dials.decision_interval_seconds,
            )
            self._admit_visible_work(
                scenario=scenario,
                policy=policy,
                now_seconds=now_seconds,
                utilization=utilization,
                queued=queued,
                active=active,
                group_service=group_service,
                last_wait_reason=last_wait_reason,
                admissions=admissions,
                wait_transitions=wait_transitions,
            )
            requested_cpu_cores = self._requested_cpu_cores(
                scenario,
                active,
                now_seconds,
            )
            maximum_requested_cpu_cores = max(
                maximum_requested_cpu_cores,
                requested_cpu_cores,
            )
            maximum_charged_cpu_slots = max(
                maximum_charged_cpu_slots,
                sum(item.grant.cpu_slots for item in active),
            )
            requested_cpu_core_seconds_above_capacity += max(
                0.0,
                requested_cpu_cores - scenario.dials.host_cpu_slots,
            ) * scenario.dials.decision_interval_seconds
            now_seconds += scenario.dials.decision_interval_seconds

        return ExecutorSimulationResult(
            admissions=tuple(admissions),
            wait_transitions=tuple(wait_transitions),
            completed_request_count=completed_request_count,
            completed_validation_count=len(completed_groups),
            finished_at_seconds=now_seconds,
            maximum_requested_cpu_cores=maximum_requested_cpu_cores,
            maximum_charged_cpu_slots=maximum_charged_cpu_slots,
            requested_cpu_core_seconds_above_capacity=(
                requested_cpu_core_seconds_above_capacity
            ),
            cooperative_yield_count=cooperative_yield_count,
            learned_demands=tuple(
                SimulatedLearnedDemand(work_key, demand)
                for work_key, demand in sorted(
                    learned_by_work.items(),
                    key=lambda item: item[0].value,
                )
            ),
        )

    @staticmethod
    def _admit_visible_work(
        *,
        scenario: ExecutorSimulationScenario,
        policy: ExecutorAdmissionPolicy,
        now_seconds: float,
        utilization: ExecutorHostCpuUtilization,
        queued: list[_QueuedSimulationWork],
        active: list[_ActiveSimulationWork],
        group_service: dict[ExecutorFairnessGroup, int],
        last_wait_reason: dict[ExecutorRequestId, ExecutorWaitReason],
        admissions: list[SimulatedAdmission],
        wait_transitions: list[SimulatedWaitTransition],
    ) -> None:
        while queued:
            snapshot = ExecutorQueueSnapshot(
                host_cpu_slots=scenario.dials.host_cpu_slots,
                queued=tuple(item.work for item in queued),
                active=tuple(item.lease() for item in active),
                group_service=tuple(
                    ExecutorGroupService(group, cpu_slots)
                    for group, cpu_slots in sorted(
                        group_service.items(),
                        key=lambda item: item[0].value,
                    )
                ),
                host_cpu_utilization=utilization,
            )
            admitted: _QueuedSimulationWork | None = None
            admission_grant: ExecutorAdmissionGrant | None = None
            for pending in queued:
                decision = policy.decide(pending.work, snapshot)
                if isinstance(decision, ExecutorAdmissionGranted):
                    admitted = pending
                    admission_grant = decision.grant
                    break
                previous_reason = last_wait_reason.get(pending.work.request_id)
                if previous_reason != decision.reason:
                    wait_transitions.append(
                        SimulatedWaitTransition(
                            request_id=pending.work.request_id,
                            at_seconds=now_seconds,
                            reason=decision.reason,
                            host_cpu_busy_percent=utilization.busy_percent,
                        )
                    )
                    last_wait_reason[pending.work.request_id] = decision.reason
            if admitted is None or admission_grant is None:
                return
            queued.remove(admitted)
            last_wait_reason.pop(admitted.work.request_id, None)
            completes_at_seconds = now_seconds + (
                admitted.remaining_serial_seconds
                + admitted.remaining_parallel_seconds_at_one_concurrency
                / admission_grant.concurrency
            )
            execution_mode = scenario.dials.execution_mode
            next_safe_boundary_at_seconds = completes_at_seconds
            if admitted.safe_boundary_intervals_seconds:
                next_safe_boundary_at_seconds = min(
                    completes_at_seconds,
                    now_seconds + admitted.safe_boundary_intervals_seconds[0],
                )
            active.append(
                _ActiveSimulationWork(
                    admitted,
                    admission_grant,
                    now_seconds,
                    completes_at_seconds,
                    next_safe_boundary_at_seconds,
                )
            )
            group_service[admitted.work.fairness_group] += admission_grant.cpu_slots
            if admitted.resume_count == 0:
                admissions.append(
                    SimulatedAdmission(
                        request_id=admitted.work.request_id,
                        work_key=admitted.work.work_key,
                        fairness_group=admitted.work.fairness_group,
                        arrived_at_seconds=admitted.arrived_at_seconds,
                        admitted_at_seconds=now_seconds,
                        completes_at_seconds=completes_at_seconds,
                        grant=admission_grant,
                    )
                )

    @classmethod
    def _yield_pressured_work(
        cls,
        scenario: ExecutorSimulationScenario,
        now_seconds: float,
        queued: list[_QueuedSimulationWork],
        active: list[_ActiveSimulationWork],
    ) -> int:
        mode = scenario.dials.execution_mode
        if isinstance(mode, SimulatedRunToCompletion):
            return 0
        yielded = 0
        for running in tuple(active):
            if running.next_safe_boundary_at_seconds > now_seconds:
                continue
            remaining_boundaries = (
                running.queued.safe_boundary_intervals_seconds[1:]
            )
            utilization = ExecutorHostCpuUtilization(
                cls._host_busy_percent(scenario, active, now_seconds),
                scenario.dials.decision_interval_seconds,
            )
            if not scenario.dials.saturation.requires_attenuation(utilization):
                active_index = active.index(running)
                next_boundary = running.completes_at_seconds
                if remaining_boundaries:
                    next_boundary = min(
                        running.completes_at_seconds,
                        now_seconds + remaining_boundaries[0],
                    )
                active[active_index] = _ActiveSimulationWork(
                    queued=_QueuedSimulationWork(
                        work=running.queued.work,
                        profile=running.queued.profile,
                        arrived_at_seconds=running.queued.arrived_at_seconds,
                        remaining_serial_seconds=(
                            running.queued.remaining_serial_seconds
                        ),
                        remaining_parallel_seconds_at_one_concurrency=(
                            running.queued.remaining_parallel_seconds_at_one_concurrency
                        ),
                        resume_count=running.queued.resume_count,
                        safe_boundary_intervals_seconds=remaining_boundaries,
                    ),
                    grant=running.grant,
                    segment_started_at_seconds=running.segment_started_at_seconds,
                    completes_at_seconds=running.completes_at_seconds,
                    next_safe_boundary_at_seconds=next_boundary,
                )
                continue
            elapsed = now_seconds - running.segment_started_at_seconds
            remaining_serial = max(
                0.0,
                running.queued.remaining_serial_seconds - elapsed,
            )
            parallel_elapsed = max(
                0.0,
                elapsed - running.queued.remaining_serial_seconds,
            )
            remaining_parallel = max(
                0.0,
                running.queued.remaining_parallel_seconds_at_one_concurrency
                - parallel_elapsed * running.grant.concurrency,
            )
            active.remove(running)
            queued.append(
                _QueuedSimulationWork(
                    work=running.queued.work,
                    profile=running.queued.profile,
                    arrived_at_seconds=running.queued.arrived_at_seconds,
                    remaining_serial_seconds=remaining_serial,
                    remaining_parallel_seconds_at_one_concurrency=remaining_parallel,
                    resume_count=running.queued.resume_count + 1,
                    safe_boundary_intervals_seconds=remaining_boundaries,
                )
            )
            yielded += 1
        return yielded

    @staticmethod
    def _requested_cpu_cores(
        scenario: ExecutorSimulationScenario,
        active: list[_ActiveSimulationWork],
        now_seconds: float,
    ) -> float:
        managed = sum(
            item.queued.profile.requested_cpu_cores(item.grant) for item in active
        )
        unmanaged = sum(
            window.demand_at(now_seconds)
            for window in scenario.external_cpu_windows
        )
        return managed + unmanaged

    @classmethod
    def _host_busy_percent(
        cls,
        scenario: ExecutorSimulationScenario,
        active: list[_ActiveSimulationWork],
        now_seconds: float,
    ) -> float:
        requested = cls._requested_cpu_cores(scenario, active, now_seconds)
        return min(100.0, 100.0 * requested / scenario.dials.host_cpu_slots)
