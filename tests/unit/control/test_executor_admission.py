"""Pure behavior tests for executor demand learning and admission policy."""

from __future__ import annotations

import pytest

from issue_orchestrator.control.executor_admission import (
    ActiveExecutorLease,
    ExecutorAdmissionGrant,
    ExecutorAdmissionDeferred,
    ExecutorAdmissionGranted,
    ExecutorAdmissionPolicy,
    ExecutorGroupService,
    ExecutorLearnedDemand,
    ExecutorLearningPolicy,
    ExecutorQueueSnapshot,
    ExecutorResourceObservation,
    ExecutorSaturationPolicy,
    ExecutorWorkDemandEstimator,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor_host import ExecutorHostCpuUtilization
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorRequestId,
    ExecutorWaitReason,
)


IDLE_HOST = ExecutorHostCpuUtilization(0.0, 1.0)
SATURATION_POLICY = ExecutorSaturationPolicy(maximum_busy_percent=95)


def _policy() -> ExecutorAdmissionPolicy:
    return ExecutorAdmissionPolicy(SATURATION_POLICY)


def _work(
    request_id: str,
    sequence: int,
    group: str,
    concurrency_range: ExecutorConcurrencyRange,
    learned_cores: float,
    aggressiveness_percent: int,
    resources: tuple[ExecutorExclusiveResource, ...],
) -> QueuedExecutorWork:
    return QueuedExecutorWork(
        request_id=ExecutorRequestId(request_id),
        sequence=sequence,
        work_key=ExecutorWorkKey(f"test:{request_id}"),
        fairness_group=ExecutorFairnessGroup(group),
        concurrency_range=concurrency_range,
        learned_demand=ExecutorLearnedDemand(learned_cores),
        aggressiveness=ExecutorAggressiveness(aggressiveness_percent),
        exclusive_resources=resources,
    )


def _snapshot(
    host_cpu_slots: int,
    queued: tuple[QueuedExecutorWork, ...],
    active: tuple[ActiveExecutorLease, ...],
    service: tuple[tuple[str, int], ...],
    *,
    host_cpu_utilization: ExecutorHostCpuUtilization = IDLE_HOST,
) -> ExecutorQueueSnapshot:
    return ExecutorQueueSnapshot(
        host_cpu_slots=host_cpu_slots,
        queued=queued,
        active=active,
        group_service=tuple(
            ExecutorGroupService(ExecutorFairnessGroup(group), capacity)
            for group, capacity in service
        ),
        host_cpu_utilization=host_cpu_utilization,
    )


def test_estimator_learns_cpu_occupancy_per_concurrency_unit() -> None:
    estimator = ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=0.5,
            minimum_cores_per_concurrency=0.1,
            recent_observation_weight=0.5,
        )
    )
    observations = (
        ExecutorResourceObservation(
            concurrency=4,
            wall_seconds=10.0,
            cpu_seconds=20.0,
            executor_process_lifetime_children_max_rss_bytes=100,
            input_blocks=0,
            output_blocks=0,
        ),
        ExecutorResourceObservation(
            concurrency=2,
            wall_seconds=10.0,
            cpu_seconds=2.0,
            executor_process_lifetime_children_max_rss_bytes=100,
            input_blocks=0,
            output_blocks=0,
        ),
    )

    assert estimator.estimate(()).cores_per_concurrency == 0.5
    assert estimator.estimate(observations).cores_per_concurrency == pytest.approx(0.3)


def test_adaptive_work_receives_largest_grant_that_fits_learned_demand() -> None:
    current = _work(
        "adaptive",
        1,
        "validation-a",
        ExecutorConcurrencyRange(2, 12),
        learned_cores=0.75,
        aggressiveness_percent=100,
        resources=(),
    )
    active = ActiveExecutorLease(
        fairness_group=ExecutorFairnessGroup("validation-b"),
        grant=ExecutorAdmissionGrant(concurrency=4, cpu_slots=4),
        exclusive_resources=(),
    )

    decision = _policy().decide(
        current,
        _snapshot(
            10,
            (current,),
            (active,),
            (("validation-a", 0), ("validation-b", 4)),
        ),
    )

    assert isinstance(decision, ExecutorAdmissionGranted)
    assert decision.grant == ExecutorAdmissionGrant(
        concurrency=8,
        cpu_slots=6,
    )


def test_oversized_range_forces_only_its_minimum_on_a_small_host() -> None:
    current = _work(
        "small-host",
        1,
        "validation-a",
        ExecutorConcurrencyRange(8, 24),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )

    decision = _policy().decide(
        current,
        _snapshot(2, (current,), (), (("validation-a", 0),)),
    )

    assert decision == ExecutorAdmissionGranted(
        grant=ExecutorAdmissionGrant(concurrency=8, cpu_slots=2),
        leased_cpu_slots_before=0,
        available_cpu_slots_before=2,
        reserved_cpu_slots_for_queued_peers=0,
    )


def test_admission_reserves_minimum_capacity_for_compatible_queued_work() -> None:
    first = _work(
        "first",
        1,
        "validation-a",
        ExecutorConcurrencyRange(2, 10),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    sibling = _work(
        "sibling",
        2,
        "validation-a",
        ExecutorConcurrencyRange(2, 6),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )

    decision = _policy().decide(
        first,
        _snapshot(
            10,
            (first, sibling),
            (),
            (("validation-a", 0),),
        ),
    )

    assert decision == ExecutorAdmissionGranted(
        grant=ExecutorAdmissionGrant(concurrency=8, cpu_slots=8),
        leased_cpu_slots_before=0,
        available_cpu_slots_before=10,
        reserved_cpu_slots_for_queued_peers=2,
    )


def test_admission_does_not_reserve_for_mutually_exclusive_queued_work() -> None:
    browser = ExecutorExclusiveResource("browser")
    first = _work(
        "first",
        1,
        "validation-a",
        ExecutorConcurrencyRange(2, 10),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(browser,),
    )
    sibling = _work(
        "sibling",
        2,
        "validation-a",
        ExecutorConcurrencyRange(2, 6),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(browser,),
    )

    decision = _policy().decide(
        first,
        _snapshot(
            10,
            (first, sibling),
            (),
            (("validation-a", 0),),
        ),
    )

    assert decision == ExecutorAdmissionGranted(
        grant=ExecutorAdmissionGrant(concurrency=10, cpu_slots=10),
        leased_cpu_slots_before=0,
        available_cpu_slots_before=10,
        reserved_cpu_slots_for_queued_peers=0,
    )


def test_admission_reserves_only_one_slot_cohort_for_exclusive_peers() -> None:
    browser = ExecutorExclusiveResource("browser")
    current = _work(
        "current",
        1,
        "validation-a",
        ExecutorConcurrencyRange(2, 16),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    browser_peers = tuple(
        _work(
            f"browser-{index}",
            index + 2,
            f"validation-{index + 2}",
            ExecutorConcurrencyRange(2, 2),
            learned_cores=1.0,
            aggressiveness_percent=100,
            resources=(browser,),
        )
        for index in range(3)
    )

    decision = _policy().decide(
        current,
        _snapshot(
            16,
            (current, *browser_peers),
            (),
            (
                ("validation-a", 0),
                ("validation-2", 0),
                ("validation-3", 0),
                ("validation-4", 0),
            ),
        ),
    )

    assert decision == ExecutorAdmissionGranted(
        grant=ExecutorAdmissionGrant(concurrency=14, cpu_slots=14),
        leased_cpu_slots_before=0,
        available_cpu_slots_before=16,
        reserved_cpu_slots_for_queued_peers=2,
    )


def test_aggressiveness_is_the_single_machine_pressure_dial() -> None:
    conservative = _work(
        "conservative",
        1,
        "conservative-run",
        ExecutorConcurrencyRange(8, 8),
        learned_cores=1.0,
        aggressiveness_percent=50,
        resources=(),
    )
    aggressive = _work(
        "aggressive",
        1,
        "aggressive-run",
        ExecutorConcurrencyRange(8, 8),
        learned_cores=1.0,
        aggressiveness_percent=200,
        resources=(),
    )

    conservative_decision = _policy().decide(
        conservative,
        _snapshot(16, (conservative,), (), (("conservative-run", 0),)),
    )
    aggressive_decision = _policy().decide(
        aggressive,
        _snapshot(16, (aggressive,), (), (("aggressive-run", 0),)),
    )

    assert isinstance(conservative_decision, ExecutorAdmissionGranted)
    assert conservative_decision.grant.cpu_slots == 16
    assert isinstance(aggressive_decision, ExecutorAdmissionGranted)
    assert aggressive_decision.grant.cpu_slots == 4


def test_fairness_prefers_the_least_served_validation_group() -> None:
    heavy = _work(
        "heavy",
        1,
        "io-validation",
        ExecutorConcurrencyRange(1, 1),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    light = _work(
        "light",
        2,
        "porchpin-validation",
        ExecutorConcurrencyRange(1, 1),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    snapshot = _snapshot(
        4,
        (heavy, light),
        (),
        (("io-validation", 4), ("porchpin-validation", 0)),
    )

    heavy_decision = _policy().decide(heavy, snapshot)
    light_decision = _policy().decide(light, snapshot)

    assert heavy_decision == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.FAIRNESS,
        leased_cpu_slots=0,
        available_cpu_slots=4,
    )
    assert isinstance(light_decision, ExecutorAdmissionGranted)


def test_admission_drains_capacity_for_oldest_same_group_request() -> None:
    too_large = _work(
        "too-large",
        1,
        "validation-a",
        ExecutorConcurrencyRange(8, 8),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    small = _work(
        "small",
        2,
        "validation-a",
        ExecutorConcurrencyRange(1, 1),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    active = ActiveExecutorLease(
        fairness_group=ExecutorFairnessGroup("validation-b"),
        grant=ExecutorAdmissionGrant(3, 3),
        exclusive_resources=(),
    )
    snapshot = _snapshot(
        4,
        (too_large, small),
        (active,),
        (("validation-a", 0), ("validation-b", 3)),
    )

    too_large_decision = _policy().decide(too_large, snapshot)
    small_decision = _policy().decide(small, snapshot)

    assert too_large_decision == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.CAPACITY,
        leased_cpu_slots=3,
        available_cpu_slots=1,
    )
    assert small_decision == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.FAIRNESS,
        leased_cpu_slots=3,
        available_cpu_slots=1,
    )


def test_admission_does_not_bypass_the_fairest_group() -> None:
    least_served_but_too_large = _work(
        "least-served",
        1,
        "validation-a",
        ExecutorConcurrencyRange(8, 8),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    served_but_fitting = _work(
        "served",
        2,
        "validation-b",
        ExecutorConcurrencyRange(1, 1),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )
    active = ActiveExecutorLease(
        fairness_group=ExecutorFairnessGroup("validation-c"),
        grant=ExecutorAdmissionGrant(3, 3),
        exclusive_resources=(),
    )
    snapshot = _snapshot(
        4,
        (least_served_but_too_large, served_but_fitting),
        (active,),
        (("validation-a", 0), ("validation-b", 2), ("validation-c", 3)),
    )

    assert _policy().decide(
        least_served_but_too_large,
        snapshot,
    ) == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.CAPACITY,
        leased_cpu_slots=3,
        available_cpu_slots=1,
    )
    assert _policy().decide(
        served_but_fitting,
        snapshot,
    ) == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.FAIRNESS,
        leased_cpu_slots=3,
        available_cpu_slots=1,
    )


def test_busy_exclusive_resource_defers_only_matching_work() -> None:
    browser = ExecutorExclusiveResource("browser")
    current = _work(
        "browser-next",
        1,
        "validation-a",
        ExecutorConcurrencyRange(1, 1),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(browser,),
    )
    active = ActiveExecutorLease(
        fairness_group=ExecutorFairnessGroup("validation-b"),
        grant=ExecutorAdmissionGrant(1, 1),
        exclusive_resources=(browser,),
    )

    decision = _policy().decide(
        current,
        _snapshot(
            4,
            (current,),
            (active,),
            (("validation-a", 0), ("validation-b", 1)),
        ),
    )

    assert decision == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.EXCLUSIVE_RESOURCE,
        leased_cpu_slots=1,
        available_cpu_slots=3,
    )


def test_measured_host_saturation_attenuates_an_otherwise_fitting_admission() -> None:
    current = _work(
        "pressure-sensitive",
        1,
        "validation-a",
        ExecutorConcurrencyRange(1, 4),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )

    active = ActiveExecutorLease(
        fairness_group=ExecutorFairnessGroup("validation-b"),
        grant=ExecutorAdmissionGrant(1, 1),
        exclusive_resources=(),
    )
    decision = _policy().decide(
        current,
        _snapshot(
            18,
            (current,),
            (active,),
            (("validation-a", 0), ("validation-b", 1)),
            host_cpu_utilization=ExecutorHostCpuUtilization(95.0, 0.1),
        ),
    )

    assert decision == ExecutorAdmissionDeferred(
        reason=ExecutorWaitReason.HOST_PRESSURE,
        leased_cpu_slots=1,
        available_cpu_slots=17,
    )


def test_measured_host_saturation_never_starves_an_idle_executor() -> None:
    current = _work(
        "idle-probe",
        1,
        "validation-a",
        ExecutorConcurrencyRange(1, 4),
        learned_cores=1.0,
        aggressiveness_percent=100,
        resources=(),
    )

    decision = _policy().decide(
        current,
        _snapshot(
            18,
            (current,),
            (),
            (("validation-a", 0),),
            host_cpu_utilization=ExecutorHostCpuUtilization(100.0, 0.1),
        ),
    )

    assert isinstance(decision, ExecutorAdmissionGranted)
    assert decision.grant.cpu_slots == 1
