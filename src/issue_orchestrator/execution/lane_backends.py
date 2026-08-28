# pyright: strict
"""The single registry of lane backends.

Three entrypoints need a backend by name — the lane runner, the gate's
policy preflight, and the operator's pool snapshot — and each needs a
*different* thing built from it. A name list beside one if-chain per
consumer is a place to add a backend per consumer and as many chances
to add it to only some: lane-run would accept a backend the preflight
rejects, which is precisely the gate stepping around its own guard
(A1, #7132 review).

So a backend is ONE registry entry carrying every factory. The
dataclass makes an entry missing any factory a TypeError at import,
the selectable names are derived from the registry rather than
declared beside it, and every builder resolves through the same
lookup. Adding a backend is one entry; there is no second place to
forget.

The scheduler adapter is imported lazily inside its factories: the
default path must not pay for importing a backend it will not use.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from ..adapters.direct_lane_executor import (
    DirectLaneExecutor,
    DirectLanePolicyCheck,
    DirectLanePoolInspector,
    DirectLaneTerminationPolicy,
)
from ..domain.lane_execution import LaneExecutorUnavailableError
from ..ports.executor_pool import ExecutorPoolInspector
from ..ports.lane_executor import LaneExecutor
from ..ports.lane_policy_check import LanePolicyCheck

BACKEND_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_LANE_EXECUTOR"
DIRECT_BACKEND = "direct"
CONDOR_BACKEND = "condor"

_DIRECT_GRACEFUL_SHUTDOWN_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class LaneBackend:
    """One selectable backend and everything the system builds from it.

    Every factory is a required field, so a half-registered backend
    cannot be constructed at all — the failure is a TypeError raised
    while this module is being imported, long before a gate could
    discover the gap by preflighting one backend and running another,
    or an operator could discover it by asking a backend that runs
    lanes to describe the pool it runs them on.
    """

    name: str
    executor_factory: Callable[[], LaneExecutor]
    policy_check_factory: Callable[[], LanePolicyCheck]
    pool_inspector_factory: Callable[[], ExecutorPoolInspector]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("LaneBackend.name must be a non-empty string")
        for field_name, factory in (
            ("executor_factory", self.executor_factory),
            ("policy_check_factory", self.policy_check_factory),
            ("pool_inspector_factory", self.pool_inspector_factory),
        ):
            if not callable(factory):
                raise ValueError(f"LaneBackend.{field_name} must be callable")


def _build_direct_executor() -> LaneExecutor:
    return DirectLaneExecutor(
        DirectLaneTerminationPolicy(_DIRECT_GRACEFUL_SHUTDOWN_SECONDS)
    )


def _build_direct_policy_check() -> LanePolicyCheck:
    return DirectLanePolicyCheck()


def _build_direct_pool_inspector() -> ExecutorPoolInspector:
    return DirectLanePoolInspector()


def _build_condor_executor() -> LaneExecutor:
    from ..adapters.condor import CondorLaneExecutor, CondorTools

    return CondorLaneExecutor(CondorTools.resolve())


def _build_condor_policy_check() -> LanePolicyCheck:
    from ..adapters.condor import CondorPoolPolicyCheck, CondorTools

    return CondorPoolPolicyCheck(CondorTools.resolve())


def _build_condor_pool_inspector() -> ExecutorPoolInspector:
    from ..adapters.condor import resolve_pool_inspector

    return resolve_pool_inspector()


def _register(*backends: LaneBackend) -> Mapping[str, LaneBackend]:
    """Index the backends by name, read-only.

    A read-only view rather than a plain dict: "one registry" is only
    true if nothing can add a backend after import, where it would be
    seen by whichever consumer happened to look next.
    """
    registry: dict[str, LaneBackend] = {}
    for backend in backends:
        if backend.name in registry:
            raise ValueError(f"duplicate lane backend {backend.name!r}")
        registry[backend.name] = backend
    return MappingProxyType(registry)


BACKENDS: Mapping[str, LaneBackend] = _register(
    LaneBackend(
        name=DIRECT_BACKEND,
        executor_factory=_build_direct_executor,
        policy_check_factory=_build_direct_policy_check,
        pool_inspector_factory=_build_direct_pool_inspector,
    ),
    LaneBackend(
        name=CONDOR_BACKEND,
        executor_factory=_build_condor_executor,
        policy_check_factory=_build_condor_policy_check,
        pool_inspector_factory=_build_condor_pool_inspector,
    ),
)

# Argparse choices for every lane entrypoint, DERIVED from the registry
# so a backend becomes selectable exactly when it becomes buildable.
BACKEND_NAMES: tuple[str, ...] = tuple(BACKENDS)


def _resolve(backend: str) -> LaneBackend:
    registered = BACKENDS.get(backend)
    if registered is None:
        raise LaneExecutorUnavailableError(f"unknown lane backend {backend!r}")
    return registered


def build_lane_executor(backend: str) -> LaneExecutor:
    return _resolve(backend).executor_factory()


def build_lane_policy_check(backend: str) -> LanePolicyCheck:
    """The backend's policy self-check — always a real one.

    Every registered backend answers, including the one whose honest
    answer is an empty invariant set, so callers never branch on the
    backend to decide whether a check exists.
    """
    return _resolve(backend).policy_check_factory()


def build_pool_inspector(backend: str) -> ExecutorPoolInspector:
    """The backend's read-only pool view — always a real one.

    Resolving an unregistered name raises, like every other builder.
    What does NOT raise is a registered backend whose pool is absent or
    unreachable: its factory answers with an inspector that reports the
    reason. The difference from :func:`build_lane_policy_check` follows
    the caller's purpose — a preflight is a gate, so an unreachable
    backend must stop the run, while a status snapshot is a report, so
    an unreachable backend is itself the finding and must be stated
    alongside the dispatch history that is still readable.
    """
    return _resolve(backend).pool_inspector_factory()
