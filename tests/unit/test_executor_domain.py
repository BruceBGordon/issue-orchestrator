"""Construction invariants for the strongly typed executor vocabulary."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from issue_orchestrator.control.executor_admission import (
    ActiveExecutorLease,
    ExecutorAdmissionGrant,
    ExecutorAdmissionGranted,
    ExecutorLearnedDemand,
    ExecutorLearningPolicy,
    ExecutorQueueSnapshot,
    ExecutorResourceObservation,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorBoundedDeadline,
    ExecutorCommand,
    ExecutorCommandLifecycle,
    ExecutorConcurrencyRange,
    ExecutorDeadlineExceededError,
    ExecutorDeadlineReason,
    ExecutorFairnessGroup,
    ExecutorInteractiveSessionCancellation,
    ExecutorNoCommandCancellation,
    ExecutorRunSpecification,
    ExecutorUnboundedDeadline,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_host import ExecutorHostCpuUtilization
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorCpuSlotState,
    ExecutorEventMetadata,
    ExecutorHostLoad,
    ExecutorMonitoredWork,
    ExecutorRequestId,
    ExecutorRepositoryReference,
    ExecutorWorkAdmitted,
)


def _valid_work() -> QueuedExecutorWork:
    return QueuedExecutorWork(
        request_id=ExecutorRequestId("request-1"),
        sequence=1,
        work_key=ExecutorWorkKey("io:unit"),
        fairness_group=ExecutorFairnessGroup("validation-1"),
        concurrency_range=ExecutorConcurrencyRange(1, 4),
        learned_demand=ExecutorLearnedDemand(0.5),
        aggressiveness=ExecutorAggressiveness(100),
        exclusive_resources=(),
    )


def test_public_run_specification_rejects_a_primitive_in_place_of_identity() -> None:
    with pytest.raises(
        ValueError,
        match="work_key must be an ExecutorWorkKey",
    ):
        ExecutorRunSpecification(
            work_key=cast(ExecutorWorkKey, "io:unit"),
            fairness_group=ExecutorFairnessGroup("validation-1"),
            concurrency_range=ExecutorConcurrencyRange(1, 4),
            exclusive_resources=(),
        )


def test_work_key_preserves_human_readable_spaces_and_unicode() -> None:
    work_key = ExecutorWorkKey("agent-phase:agent:backend team · β:code")

    assert work_key.value == "agent-phase:agent:backend team · β:code"


@pytest.mark.parametrize("value", ("", "   ", "line\nbreak", "x" * 161))
def test_work_key_rejects_non_human_readable_values(value: str) -> None:
    with pytest.raises(ValueError, match="printable Unicode"):
        ExecutorWorkKey(value)


def test_interactive_cancellation_requires_an_absolute_record_path() -> None:
    with pytest.raises(ValueError, match="absolute Path"):
        ExecutorInteractiveSessionCancellation(Path("relative.json"))


@pytest.mark.parametrize(
    ("lifecycle", "cancellation"),
    (
        (
            ExecutorCommandLifecycle.DETACHED,
            ExecutorInteractiveSessionCancellation(Path("/tmp/cancellation.json")),
        ),
        (
            ExecutorCommandLifecycle.INTERACTIVE_SESSION,
            ExecutorNoCommandCancellation(),
        ),
    ),
)
def test_command_rejects_a_cancellation_contract_for_another_lifecycle(
    lifecycle: ExecutorCommandLifecycle,
    cancellation: ExecutorNoCommandCancellation
    | ExecutorInteractiveSessionCancellation,
) -> None:
    with pytest.raises(ValueError, match="lifecycle and cancellation"):
        ExecutorCommand(
            ("true",),
            ExecutorUnboundedDeadline(),
            lifecycle,
            cancellation,
        )


def test_learning_contract_rejects_integer_where_float_is_required() -> None:
    with pytest.raises(
        ValueError,
        match="wall_seconds must be finite and positive",
    ):
        ExecutorResourceObservation(
            concurrency=1,
            wall_seconds=cast(float, 1),
            cpu_seconds=0.5,
            guardian_process_lifetime_children_max_rss_bytes=0,
            input_blocks=0,
            output_blocks=0,
        )

    with pytest.raises(
        ValueError,
        match="recent_observation_weight must be in",
    ):
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=0.5,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=cast(float, 1),
        )


def test_queue_contract_rejects_untyped_nested_values() -> None:
    valid = _valid_work()
    with pytest.raises(
        ValueError, match="request_id must have exact type ExecutorRequestId"
    ):
        QueuedExecutorWork(
            request_id=cast(ExecutorRequestId, "request-1"),
            sequence=valid.sequence,
            work_key=valid.work_key,
            fairness_group=valid.fairness_group,
            concurrency_range=valid.concurrency_range,
            learned_demand=valid.learned_demand,
            aggressiveness=valid.aggressiveness,
            exclusive_resources=valid.exclusive_resources,
        )

    with pytest.raises(ValueError, match="active must have exact type tuple"):
        ExecutorQueueSnapshot(
            host_cpu_slots=4,
            queued=(valid,),
            active=cast(tuple[ActiveExecutorLease, ...], []),
            group_service=(),
            host_cpu_utilization=ExecutorHostCpuUtilization(0.0, 1.0),
        )


def test_active_lease_requires_a_typed_grant() -> None:
    with pytest.raises(
        ValueError,
        match="grant must have exact type ExecutorAdmissionGrant",
    ):
        ActiveExecutorLease(
            fairness_group=ExecutorFairnessGroup("validation-1"),
            grant=cast(ExecutorAdmissionGrant, (1, 1)),
            exclusive_resources=(),
        )


def test_admission_grant_rejects_reservation_beyond_available_capacity() -> None:
    with pytest.raises(ValueError, match="grant plus peer reservation"):
        ExecutorAdmissionGranted(
            grant=ExecutorAdmissionGrant(concurrency=3, cpu_slots=3),
            leased_cpu_slots_before=1,
            available_cpu_slots_before=3,
            reserved_cpu_slots_for_queued_peers=1,
        )


def test_admitted_event_rejects_impossible_capacity_evidence() -> None:
    with pytest.raises(ValueError, match="charged plus reserved CPU slots"):
        ExecutorWorkAdmitted(
            metadata=ExecutorEventMetadata(1.0, 1),
            work=ExecutorMonitoredWork(
                request_id=ExecutorRequestId("request-1"),
                repository=ExecutorRepositoryReference("repository", "repository"),
                work_key=ExecutorWorkKey("io:unit"),
                fairness_group=ExecutorFairnessGroup("validation-1"),
            ),
            concurrency=3,
            charged_cpu_slots=3,
            reserved_cpu_slots_for_queued_peers=1,
            cpu_slots_before=ExecutorCpuSlotState(1, 3, 4),
            wait_seconds=0.0,
            host_load=ExecutorHostLoad(0.0, 0.0, 0.0),
            host_cpu_utilization=ExecutorHostCpuUtilization(0.0, 1.0),
        )


def test_bounded_deadline_excludes_queue_time_from_active_budget() -> None:
    deadline = ExecutorBoundedDeadline(
        active_timeout_seconds=60.0,
        absolute_timeout_seconds=120.0,
    )

    budget = deadline.command_budget(
        submitted_at_monotonic=100.0,
        admitted_at_monotonic=130.0,
    )

    assert budget.timeout_seconds == 60.0
    assert budget.reason is ExecutorDeadlineReason.ACTIVE


def test_bounded_deadline_uses_independent_absolute_escape_valve() -> None:
    deadline = ExecutorBoundedDeadline(
        active_timeout_seconds=60.0,
        absolute_timeout_seconds=120.0,
    )

    budget = deadline.command_budget(
        submitted_at_monotonic=100.0,
        admitted_at_monotonic=180.0,
    )

    assert budget.timeout_seconds == 40.0
    assert budget.reason is ExecutorDeadlineReason.ABSOLUTE


def test_bounded_deadline_fails_before_work_after_absolute_expiry() -> None:
    deadline = ExecutorBoundedDeadline(
        active_timeout_seconds=60.0,
        absolute_timeout_seconds=120.0,
    )

    with pytest.raises(
        ExecutorDeadlineExceededError,
        match="expired while awaiting admission",
    ) as queued:
        deadline.require_pending_at(
            submitted_at_monotonic=100.0,
            observed_at_monotonic=220.0,
        )
    with pytest.raises(
        ExecutorDeadlineExceededError,
        match="expired at admission",
    ) as admitted:
        deadline.command_budget(
            submitted_at_monotonic=100.0,
            admitted_at_monotonic=220.0,
        )

    assert queued.value.reason is ExecutorDeadlineReason.ABSOLUTE
    assert admitted.value.reason is ExecutorDeadlineReason.ABSOLUTE


def test_bounded_deadline_rejects_a_monotonic_clock_rollback() -> None:
    deadline = ExecutorBoundedDeadline(
        active_timeout_seconds=60.0,
        absolute_timeout_seconds=120.0,
    )

    with pytest.raises(ValueError, match="must not precede"):
        deadline.command_budget(
            submitted_at_monotonic=100.0,
            admitted_at_monotonic=99.0,
        )
