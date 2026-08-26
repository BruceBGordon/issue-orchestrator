"""Port for observing cumulative child-process resources."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.executor_child_resources import ExecutorChildResourceSnapshot


@runtime_checkable
class ExecutorChildResourceObserver(Protocol):
    """Observe one exact cumulative child-resource snapshot."""

    def observe(self) -> ExecutorChildResourceSnapshot: ...
