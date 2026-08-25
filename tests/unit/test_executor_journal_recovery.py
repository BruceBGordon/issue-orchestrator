"""Crash-tail recovery proofs for the machine-wide executor event journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorLearnedDemand,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorPolicy,
    ExecutorPolicyChange,
    ExecutorPolicySource,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorCommandLifecycleFailed,
    ExecutorRecentEventsQuery,
    ExecutorRequestId,
)
from issue_orchestrator.execution.host_executor._journal import ExecutorEventStore
from issue_orchestrator.execution.host_executor._types import (
    ExecutorRepositoryIdentity,
    ExecutorWorkIdentity,
)


def _policy_change(percent: int) -> ExecutorPolicyChange:
    policy = ExecutorPolicy(
        ExecutorAggressiveness(percent),
        ExecutorPolicySource.PERSISTED,
    )
    return ExecutorPolicyChange(saved=policy, effective=policy)


def test_torn_final_event_is_ignored_then_repaired_before_append(
    tmp_path: Path,
) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    with store.path.open("ab") as handle:
        handle.write(b'{"schema_version":4,"event":"policy-changed"')

    before_repair = store.recent_events(ExecutorRecentEventsQuery(10))
    assert len(before_repair.events) == 1

    store.policy_changed(_policy_change(125))

    after_repair = store.recent_events(ExecutorRecentEventsQuery(10))
    assert len(after_repair.events) == 2
    assert store.path.read_bytes().endswith(b"\n")


def test_invalid_interior_event_remains_a_hard_failure(tmp_path: Path) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    with store.path.open("ab") as handle:
        handle.write(b"not-json\n")
    store.policy_changed(_policy_change(125))

    with pytest.raises(RuntimeError, match="invalid executor event"):
        store.recent_events(ExecutorRecentEventsQuery(10))


def test_torn_rotated_event_is_a_hard_failure(tmp_path: Path) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    rotated = store.path.with_suffix(".jsonl.1")
    store.path.replace(rotated)
    with rotated.open("ab") as handle:
        handle.write(b'{"schema_version":4,"event":"policy-changed"')

    with pytest.raises(RuntimeError, match="invalid executor event"):
        store.recent_events(ExecutorRecentEventsQuery(10))


def test_empty_lifecycle_exception_message_remains_durable(tmp_path: Path) -> None:
    store = ExecutorEventStore(tmp_path)
    work = QueuedExecutorWork(
        request_id=ExecutorRequestId("empty-error-message"),
        sequence=1,
        work_key=ExecutorWorkKey("io:empty-error-message"),
        fairness_group=ExecutorFairnessGroup("validation-empty-error"),
        concurrency_range=ExecutorConcurrencyRange(1, 1),
        learned_demand=ExecutorLearnedDemand(1.0),
        aggressiveness=ExecutorAggressiveness(100),
        exclusive_resources=(),
    )
    identity = ExecutorWorkIdentity(
        ExecutorRepositoryIdentity((tmp_path / ".git").resolve(), "journal-test"),
        work.work_key,
    )

    store.command_lifecycle_failed(
        identity,
        work,
        ExecutorAdmissionGrant(1, 1),
        RuntimeError(),
    )

    [event] = store.recent_events(ExecutorRecentEventsQuery(10)).events
    assert type(event) is ExecutorCommandLifecycleFailed
    assert event.error_type == "RuntimeError"
    assert event.error_message == "RuntimeError()"
