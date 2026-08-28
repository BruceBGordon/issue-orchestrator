# pyright: strict
"""HTCondor lane-executor backend (anti-corruption layer).

Everything scheduler-specific lives inside this package: the outbound
submit-description compiler, the inbound job-event classifier, the
inbound pool-inspection translator, the pool-policy self-check, and the
adapters that drive them. Scheduler vocabulary must never appear outside
``adapters/condor`` — the lane contracts in ``domain/lane_execution`` and
the pool contracts in ``ports/executor_pool`` are the only language that
crosses the ports.
"""

from .lane_executor import CondorLaneExecutor
from .pool_inspector import CondorPoolInspector, resolve_pool_inspector
from .pool_policy import CondorPoolPolicyCheck
from .tools import CondorTools

__all__ = [
    "CondorLaneExecutor",
    "CondorPoolInspector",
    "CondorPoolPolicyCheck",
    "CondorTools",
    "resolve_pool_inspector",
]
