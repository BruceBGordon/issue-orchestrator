# pyright: strict
"""The single owner of lane-backend selection.

Two entrypoints now need a backend by name — the lane runner and the
gate's policy preflight — and a second mapping would be a second place
for the choice to drift. One owner also confines backend vocabulary to
one composition module plus the adapter package it names, which is
what the condor-vocabulary guardrail enforces.

The scheduler adapter is imported lazily inside its builders: the
default path must not pay for importing a backend it will not use.
"""

from __future__ import annotations

from ..adapters.direct_lane_executor import (
    DirectLaneExecutor,
    DirectLanePolicyCheck,
    DirectLaneTerminationPolicy,
)
from ..domain.lane_execution import LaneExecutorUnavailableError
from ..ports.lane_executor import LaneExecutor
from ..ports.lane_policy_check import LanePolicyCheck

BACKEND_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_LANE_EXECUTOR"
DIRECT_BACKEND = "direct"
CONDOR_BACKEND = "condor"
# Argparse choices for every lane entrypoint, so a new backend becomes
# selectable everywhere at once or nowhere.
BACKEND_NAMES: tuple[str, ...] = (DIRECT_BACKEND, CONDOR_BACKEND)

_DIRECT_GRACEFUL_SHUTDOWN_SECONDS = 10.0


def build_lane_executor(backend: str) -> LaneExecutor:
    if backend == DIRECT_BACKEND:
        return DirectLaneExecutor(
            DirectLaneTerminationPolicy(_DIRECT_GRACEFUL_SHUTDOWN_SECONDS)
        )
    if backend == CONDOR_BACKEND:
        from ..adapters.condor import CondorLaneExecutor, CondorTools

        return CondorLaneExecutor(CondorTools.resolve())
    raise LaneExecutorUnavailableError(f"unknown lane backend {backend!r}")


def build_lane_policy_check(backend: str) -> LanePolicyCheck:
    """The backend's policy self-check — always a real one.

    Every backend answers, including the one whose honest answer is an
    empty invariant set, so callers never branch on the backend to
    decide whether a check exists.
    """
    if backend == DIRECT_BACKEND:
        return DirectLanePolicyCheck()
    if backend == CONDOR_BACKEND:
        from ..adapters.condor import CondorPoolPolicyCheck, CondorTools

        return CondorPoolPolicyCheck(CondorTools.resolve())
    raise LaneExecutorUnavailableError(f"unknown lane backend {backend!r}")
