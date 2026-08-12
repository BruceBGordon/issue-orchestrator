"""Runtime composition for the Repository Engine supervisor port."""

from ..adapters.repository_engine_supervisor import DefaultSupervisorOps
from ..ports.repository_engine_supervisor import SupervisorOps


def build_default_supervisor_ops() -> SupervisorOps:
    """Wire the production process-supervisor adapter to its behavior port."""
    return DefaultSupervisorOps()


__all__ = ["build_default_supervisor_ops"]
