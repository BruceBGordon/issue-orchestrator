"""Read-only host executor monitoring adapter."""

from __future__ import annotations

from pathlib import Path

from ...domain.executor_monitoring import (
    ExecutorEventTimeline,
    ExecutorEventPage,
    ExecutorFairnessGroupEventsQuery,
    ExecutorRecentEventsQuery,
    ExecutorStatus,
    ExecutorStatusQuery,
)
from ...domain.executor import ExecutorHistoryRetentionPolicy
from ...control.executor_admission import ExecutorWorkDemandEstimator
from ...ports.executor_monitor import ExecutorMonitor
from ...ports.executor_history_lock import ExecutorHistoryRetentionLock
from ...ports.atomic_path_replacement import AtomicPathReplacement
from ._journal import ExecutorEventStore
from ._history import ExecutorWorkHistoryStore
from .host_policy import ExecutorPolicyStore
from ..atomic_record_store import ExecutorAtomicRecordStore


def _require_history_retention_lock(value: object) -> None:
    if not isinstance(value, ExecutorHistoryRetentionLock):
        raise ValueError(
            "HostExecutorMonitor.history_retention_lock must implement "
            "ExecutorHistoryRetentionLock"
        )


def _require_atomic_path_replacement(value: object) -> None:
    if not isinstance(value, AtomicPathReplacement):
        raise ValueError(
            "HostExecutorMonitor.atomic_path_replacement must implement "
            "AtomicPathReplacement"
        )


class HostExecutorMonitor(ExecutorMonitor):
    """Expose typed activity without leaking event-store persistence."""

    def __init__(
        self,
        pool_dir: Path,
        host_cpu_slots: int,
        demand_estimator: ExecutorWorkDemandEstimator,
        history_retention_policy: ExecutorHistoryRetentionPolicy,
        history_retention_lock: ExecutorHistoryRetentionLock,
        atomic_path_replacement: AtomicPathReplacement,
    ) -> None:
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError("HostExecutorMonitor.host_cpu_slots must be positive")
        _require_history_retention_lock(history_retention_lock)
        _require_atomic_path_replacement(atomic_path_replacement)
        pool_records = ExecutorAtomicRecordStore(
            pool_dir,
            atomic_path_replacement,
        )
        self._event_store = ExecutorEventStore(pool_dir)
        self._history = ExecutorWorkHistoryStore(
            pool_dir / "work-history",
            history_retention_policy,
            history_retention_lock,
            ExecutorAtomicRecordStore(
                pool_dir / "work-history",
                atomic_path_replacement,
            ),
        )
        self._policy_store = ExecutorPolicyStore(pool_dir, pool_records)
        self._host_cpu_slots = host_cpu_slots
        self._demand_estimator = demand_estimator

    def recent_events(
        self,
        query: ExecutorRecentEventsQuery,
    ) -> ExecutorEventTimeline:
        return self._event_store.recent_events(query)

    def events_for_group(
        self,
        query: ExecutorFairnessGroupEventsQuery,
    ) -> ExecutorEventPage:
        return self._event_store.events_for_group(query)

    def status(self, query: ExecutorStatusQuery) -> ExecutorStatus:
        return ExecutorStatus(
            host_cpu_slots=self._host_cpu_slots,
            policy=self._policy_store.effective(),
            learning=self._history.snapshot(self._demand_estimator, query),
        )
