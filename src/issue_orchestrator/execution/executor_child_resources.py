"""System adapter for cumulative executor child resources."""

from __future__ import annotations

import resource
import sys

from ..domain.executor_child_resources import ExecutorChildResourceSnapshot


class SystemExecutorChildResourceObserver:
    """Read process-global exited-child counters from the host kernel."""

    def observe(self) -> ExecutorChildResourceSnapshot:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        raw_max_rss = usage.ru_maxrss
        return ExecutorChildResourceSnapshot(
            user_cpu_seconds=usage.ru_utime,
            system_cpu_seconds=usage.ru_stime,
            process_lifetime_children_max_rss_bytes=(
                raw_max_rss if sys.platform == "darwin" else raw_max_rss * 1024
            ),
            input_blocks=usage.ru_inblock,
            output_blocks=usage.ru_oublock,
        )
