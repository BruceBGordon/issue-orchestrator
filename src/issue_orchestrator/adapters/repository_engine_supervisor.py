"""Production adapter for the Repository Engine supervisor port."""

from __future__ import annotations

from typing import Any, Sequence

from ..ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    RepositoryEngineLock,
    SupervisorStatus,
)


class DefaultSupervisorOps:
    """Delegate behavior-port calls to the process supervisor module."""

    def start(self, *args: Any, **kwargs: Any) -> RepositoryEngineLock:
        from ..infra import supervisor

        return supervisor.start(*args, **kwargs)

    def stop(self, *args: Any, **kwargs: Any) -> bool:
        from ..infra import supervisor

        return supervisor.stop(*args, **kwargs)

    def stop_tracked_instance(
        self,
        repo_root: Any,
        tracked: SupervisorStatus,
        *,
        reason: str,
        actor: str,
    ) -> bool:
        from ..infra import supervisor

        return supervisor.stop_tracked_instance(
            repo_root,
            tracked,
            reason=reason,
            actor=actor,
        )

    def stop_by_port(self, *args: Any, **kwargs: Any) -> bool:
        from ..infra import supervisor

        return supervisor.stop_by_port(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> SupervisorStatus:
        from ..infra import supervisor

        return supervisor.status(*args, **kwargs)

    def start_instances(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[RepositoryEngineLock]:
        from ..infra import supervisor

        return supervisor.start_instances(*args, **kwargs)

    def stop_all_instances(self, *args: Any, **kwargs: Any) -> int:
        from ..infra import supervisor

        return supervisor.stop_all_instances(*args, **kwargs)

    def status_all_instances(
        self, *args: Any, **kwargs: Any
    ) -> MultiInstanceStatus:
        from ..infra import supervisor

        return supervisor.status_all_instances(*args, **kwargs)


__all__ = ["DefaultSupervisorOps"]
