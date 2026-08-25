"""Read-only host executor monitoring adapter."""

from __future__ import annotations

from pathlib import Path

from ...domain.executor_monitoring import (
    ExecutorEventTimeline,
    ExecutorRecentEventsQuery,
    ExecutorStatus,
)
from ...control.executor_admission import ExecutorWorkDemandEstimator
from ...ports.executor_monitor import ExecutorMonitor
from ._journal import ExecutorEventStore
from ._history import ExecutorWorkHistoryStore
from .host_policy import ExecutorPolicyStore


class HostExecutorMonitor(ExecutorMonitor):
    """Expose typed activity without leaking event-store persistence."""

    def __init__(
        self,
        pool_dir: Path,
        host_cpu_slots: int,
        demand_estimator: ExecutorWorkDemandEstimator,
    ) -> None:
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError("HostExecutorMonitor.host_cpu_slots must be positive")
        self._event_store = ExecutorEventStore(pool_dir)
        self._history = ExecutorWorkHistoryStore(pool_dir / "work-history")
        self._policy_store = ExecutorPolicyStore(pool_dir)
        self._host_cpu_slots = host_cpu_slots
        self._demand_estimator = demand_estimator

    def recent_events(
        self,
        query: ExecutorRecentEventsQuery,
    ) -> ExecutorEventTimeline:
        return self._event_store.recent_events(query)

    def status(self) -> ExecutorStatus:
        return ExecutorStatus(
            host_cpu_slots=self._host_cpu_slots,
            policy=self._policy_store.effective(),
            learning=self._history.snapshot(self._demand_estimator),
        )
