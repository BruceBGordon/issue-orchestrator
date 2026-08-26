"""Port for interval-based whole-host CPU observation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.executor_host import ExecutorHostCpuUtilization


@runtime_checkable
class HostCpuUtilizationObserver(Protocol):
    """Observe host CPU pressure without exposing OS counter mechanics."""

    def reset(self) -> None:
        """Begin a new observation interval."""
        ...

    def observe(self) -> ExecutorHostCpuUtilization:
        """Finish the interval, return utilization, and begin the next one."""
        ...
