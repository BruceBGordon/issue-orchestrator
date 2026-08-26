"""Host implementation of the machine-wide :class:`Executor` port.

Only this facade is public. Queue files, locks, learning records, resource
measurements, and diagnostic schemas are implementation details.
"""

from .adapter import HostExecutor
from .monitor import HostExecutorMonitor
from .request_identity import ExecutorRequestIdentityFactory
from .host_policy import (
    default_executor_pool_dir,
    detected_executor_cpu_count,
)

__all__ = [
    "HostExecutor",
    "HostExecutorMonitor",
    "ExecutorRequestIdentityFactory",
    "default_executor_pool_dir",
    "detected_executor_cpu_count",
]
