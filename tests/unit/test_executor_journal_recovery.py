"""Crash-tail recovery proofs for the machine-wide executor event journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorPolicy,
    ExecutorPolicyChange,
    ExecutorPolicySource,
)
from issue_orchestrator.domain.executor_monitoring import ExecutorRecentEventsQuery
from issue_orchestrator.execution.host_executor._journal import ExecutorEventStore


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
