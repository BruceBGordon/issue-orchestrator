"""Virtual-time pressure simulations using the real executor admission policy."""

from __future__ import annotations

from issue_orchestrator.control.executor_admission import (
    ExecutorLearningPolicy,
    ExecutorSaturationPolicy,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import ExecutorWaitReason
from tests.unit.executor_simulation_dsl import (
    ExecutorSimulationDials,
    ExecutorSimulationScenario,
    ExecutorWorkloadSimulator,
    SimulatedArrivalSchedule,
    SimulatedCooperativeBoundaries,
    SimulatedExternalCpuWindow,
    SimulatedLaneProfile,
    SimulatedRunToCompletion,
)


SATURATION = ExecutorSaturationPolicy(maximum_busy_percent=95)
LEARNING = ExecutorLearningPolicy(
    cold_start_cores_per_concurrency=1.0,
    minimum_cores_per_concurrency=0.05,
    recent_observation_weight=0.3,
)
BROWSER = ExecutorExclusiveResource("browser")
CLAUDE = ExecutorExclusiveResource("claude")
CODEX = ExecutorExclusiveResource("codex")


IO_VALIDATION_LANES = (
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:static-v2"),
        concurrency_range=ExecutorConcurrencyRange(1, 3),
        learned_cores_per_concurrency=0.579,
        actual_cores_per_concurrency=0.579,
        serial_seconds=5.0,
        parallel_seconds_at_one_concurrency=120.0,
        exclusive_resources=(),
    ),
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:unit"),
        concurrency_range=ExecutorConcurrencyRange(8, 24),
        learned_cores_per_concurrency=0.377,
        actual_cores_per_concurrency=0.377,
        serial_seconds=5.0,
        parallel_seconds_at_one_concurrency=594.0,
        exclusive_resources=(),
    ),
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:simulated-core"),
        concurrency_range=ExecutorConcurrencyRange(4, 8),
        learned_cores_per_concurrency=0.36,
        actual_cores_per_concurrency=0.36,
        serial_seconds=5.0,
        parallel_seconds_at_one_concurrency=100.0,
        exclusive_resources=(),
    ),
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:integration-core"),
        concurrency_range=ExecutorConcurrencyRange(2, 4),
        learned_cores_per_concurrency=0.52,
        actual_cores_per_concurrency=0.52,
        serial_seconds=15.0,
        parallel_seconds_at_one_concurrency=120.0,
        exclusive_resources=(),
    ),
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:provider-claude"),
        concurrency_range=ExecutorConcurrencyRange(1, 2),
        learned_cores_per_concurrency=0.225,
        actual_cores_per_concurrency=0.225,
        serial_seconds=80.0,
        parallel_seconds_at_one_concurrency=0.0,
        exclusive_resources=(CLAUDE,),
    ),
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:provider-codex"),
        concurrency_range=ExecutorConcurrencyRange(2, 3),
        learned_cores_per_concurrency=0.102,
        actual_cores_per_concurrency=0.102,
        serial_seconds=80.0,
        parallel_seconds_at_one_concurrency=0.0,
        exclusive_resources=(CODEX,),
    ),
    SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:web"),
        concurrency_range=ExecutorConcurrencyRange(4, 12),
        learned_cores_per_concurrency=0.249,
        actual_cores_per_concurrency=0.249,
        serial_seconds=5.0,
        parallel_seconds_at_one_concurrency=240.0,
        exclusive_resources=(BROWSER,),
    ),
)


def _dials(
    *,
    aggressiveness_percent: int,
    maximum_simulation_seconds: float,
    execution_mode: SimulatedRunToCompletion | SimulatedCooperativeBoundaries,
    decision_interval_seconds: float,
) -> ExecutorSimulationDials:
    return ExecutorSimulationDials(
        host_cpu_slots=18,
        aggressiveness=ExecutorAggressiveness(aggressiveness_percent),
        saturation=SATURATION,
        learning=LEARNING,
        execution_mode=execution_mode,
        decision_interval_seconds=decision_interval_seconds,
        maximum_simulation_seconds=maximum_simulation_seconds,
    )


def test_sparse_arrivals_model_ten_lane_microbursts_over_thirty_minutes() -> None:
    scenario = ExecutorSimulationScenario(
        dials=_dials(
            aggressiveness_percent=125,
            maximum_simulation_seconds=2_000.0,
            execution_mode=SimulatedCooperativeBoundaries(
                tuple(float(value) for value in range(5, 181, 5))
            ),
            decision_interval_seconds=1.0,
        ),
        arrivals=SimulatedArrivalSchedule.evenly_spaced(
            validation_count=10,
            horizon_seconds=1_800.0,
        ),
        lanes=IO_VALIDATION_LANES,
        external_cpu_windows=(),
    )

    result = ExecutorWorkloadSimulator().run(scenario)

    assert result.completed_validation_count == 10
    assert result.completed_request_count == 70
    assert result.maximum_charged_cpu_slots <= 18
    assert result.maximum_requested_cpu_cores < 18
    assert result.maximum_queue_seconds == 0
    assert result.cooperative_yield_count > 0
    assert all(
        transition.reason is not ExecutorWaitReason.HOST_PRESSURE
        for transition in result.wait_transitions
    )


def test_external_saturation_attenuates_then_recovers_in_virtual_seconds() -> None:
    scenario = ExecutorSimulationScenario(
        dials=_dials(
            aggressiveness_percent=125,
            maximum_simulation_seconds=200.0,
            execution_mode=SimulatedRunToCompletion(),
            decision_interval_seconds=1.0,
        ),
        arrivals=SimulatedArrivalSchedule((0.0,)),
        lanes=IO_VALIDATION_LANES,
        external_cpu_windows=(
            SimulatedExternalCpuWindow(
                starts_at_seconds=0.0,
                ends_at_seconds=30.0,
                requested_cpu_cores=18.0,
            ),
        ),
    )

    result = ExecutorWorkloadSimulator().run(scenario)

    pressure_waits = tuple(
        transition
        for transition in result.wait_transitions
        if transition.reason is ExecutorWaitReason.HOST_PRESSURE
    )
    assert pressure_waits
    assert all(wait.host_cpu_busy_percent == 100.0 for wait in pressure_waits)
    first_admission, *recovered_admissions = result.admissions
    assert first_admission.admitted_at_seconds == 0.0
    assert first_admission.grant.concurrency == 1
    assert all(
        admission.admitted_at_seconds >= 30.0
        for admission in recovered_admissions
    )
    assert result.completed_validation_count == 1
    assert result.maximum_charged_cpu_slots <= 18


def test_overaggressive_microburst_is_bounded_and_next_run_waits_for_recovery() -> None:
    scenario = ExecutorSimulationScenario(
        dials=_dials(
            aggressiveness_percent=175,
            maximum_simulation_seconds=250.0,
            execution_mode=SimulatedRunToCompletion(),
            decision_interval_seconds=1.0,
        ),
        arrivals=SimulatedArrivalSchedule((0.0, 5.0)),
        lanes=IO_VALIDATION_LANES,
        external_cpu_windows=(),
    )

    result = ExecutorWorkloadSimulator().run(scenario)

    assert result.maximum_requested_cpu_cores > 18
    assert result.maximum_charged_cpu_slots <= 18
    assert any(
        transition.reason is ExecutorWaitReason.HOST_PRESSURE
        for transition in result.wait_transitions
    )
    assert result.maximum_queue_seconds > 0
    assert result.completed_validation_count == 2


def test_sparse_repetitions_learn_a_light_opaque_work_key() -> None:
    light_lane = SimulatedLaneProfile(
        work_key=ExecutorWorkKey("porchpin:checks"),
        concurrency_range=ExecutorConcurrencyRange(1, 8),
        learned_cores_per_concurrency=1.0,
        actual_cores_per_concurrency=0.2,
        serial_seconds=1.0,
        parallel_seconds_at_one_concurrency=8.0,
        exclusive_resources=(),
    )
    scenario = ExecutorSimulationScenario(
        dials=_dials(
            aggressiveness_percent=100,
            maximum_simulation_seconds=700.0,
            execution_mode=SimulatedRunToCompletion(),
            decision_interval_seconds=1.0,
        ),
        arrivals=SimulatedArrivalSchedule.evenly_spaced(
            validation_count=10,
            horizon_seconds=600.0,
        ),
        lanes=(light_lane,),
        external_cpu_windows=(),
    )

    result = ExecutorWorkloadSimulator().run(scenario)

    learned = result.learned_demands[0]
    assert learned.work_key == light_lane.work_key
    assert light_lane.actual_cores_per_concurrency < learned.cores_per_concurrency < 1
    assert result.completed_validation_count == 10


def test_application_safe_boundaries_reduce_mid_command_external_overload() -> None:
    cpu_lane = SimulatedLaneProfile(
        work_key=ExecutorWorkKey("io:cpu-heavy"),
        concurrency_range=ExecutorConcurrencyRange(1, 18),
        learned_cores_per_concurrency=1.0,
        actual_cores_per_concurrency=1.0,
        serial_seconds=0.0,
        parallel_seconds_at_one_concurrency=1_800.0,
        exclusive_resources=(),
    )
    arrivals = SimulatedArrivalSchedule((0.0,))
    external_cpu = (
        SimulatedExternalCpuWindow(
            starts_at_seconds=10.0,
            ends_at_seconds=40.0,
            requested_cpu_cores=18.0,
        ),
    )
    run_to_completion = ExecutorSimulationScenario(
        dials=_dials(
            aggressiveness_percent=100,
            maximum_simulation_seconds=200.0,
            execution_mode=SimulatedRunToCompletion(),
            decision_interval_seconds=0.1,
        ),
        arrivals=arrivals,
        lanes=(cpu_lane,),
        external_cpu_windows=external_cpu,
    )
    cooperative = ExecutorSimulationScenario(
        dials=_dials(
            aggressiveness_percent=100,
            maximum_simulation_seconds=200.0,
            execution_mode=SimulatedCooperativeBoundaries(
                tuple(float(value) for value in range(5, 101, 5))
            ),
            decision_interval_seconds=0.1,
        ),
        arrivals=arrivals,
        lanes=(cpu_lane,),
        external_cpu_windows=external_cpu,
    )

    atomic_result = ExecutorWorkloadSimulator().run(run_to_completion)
    cooperative_result = ExecutorWorkloadSimulator().run(cooperative)

    assert atomic_result.cooperative_yield_count == 0
    assert cooperative_result.cooperative_yield_count > 0
    assert (
        cooperative_result.requested_cpu_core_seconds_above_capacity
        < atomic_result.requested_cpu_core_seconds_above_capacity / 4
    )
    assert cooperative_result.completed_request_count == 1
