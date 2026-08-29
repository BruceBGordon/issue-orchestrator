# pyright: strict
"""The single registry of lane backends.

Two entrypoints need a backend by name — the lane runner and the gate's
policy preflight — and each needs a *different* thing built from it. A
name list beside one if-chain per consumer is three places to add a
backend and three chances to add it to only two: lane-run would accept
a backend the preflight rejects, which is precisely the gate stepping
around its own guard (A1, #7132 review).

So a backend is ONE registry entry carrying both factories. The
dataclass makes an entry missing either factory a TypeError at import,
the selectable names are derived from the registry rather than
declared beside it, and both builders resolve through the same lookup.
Adding a backend is one entry; there is no second place to forget.

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
    DirectLaneTerminationPolicy,
)
from ..domain.lane_execution import LaneExecutorUnavailableError
from ..ports.lane_executor import LaneExecutor
from ..ports.lane_policy_check import LanePolicyCheck

BACKEND_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_LANE_EXECUTOR"
DIRECT_BACKEND = "direct"
CONDOR_BACKEND = "condor"

_DIRECT_GRACEFUL_SHUTDOWN_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class LaneBackend:
    """One selectable backend and everything the system builds from it.

    Both factories are required fields, so a half-registered backend
    cannot be constructed at all — the failure is a TypeError raised
    while this module is being imported, long before a gate could
    discover the gap by preflighting one backend and running another.
    """

    name: str
    executor_factory: Callable[[], LaneExecutor]
    policy_check_factory: Callable[[], LanePolicyCheck]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("LaneBackend.name must be a non-empty string")
        for field_name, factory in (
            ("executor_factory", self.executor_factory),
            ("policy_check_factory", self.policy_check_factory),
        ):
            if not callable(factory):
                raise ValueError(f"LaneBackend.{field_name} must be callable")


def _build_direct_executor() -> LaneExecutor:
    return DirectLaneExecutor(
        DirectLaneTerminationPolicy(_DIRECT_GRACEFUL_SHUTDOWN_SECONDS)
    )


def _build_direct_policy_check() -> LanePolicyCheck:
    return DirectLanePolicyCheck()


def _build_condor_executor() -> LaneExecutor:
    from ..adapters.condor import CondorLaneExecutor, CondorTools

    return CondorLaneExecutor(CondorTools.resolve())


def _build_condor_policy_check() -> LanePolicyCheck:
    from ..adapters.condor import CondorPoolPolicyCheck, CondorTools

    return CondorPoolPolicyCheck(CondorTools.resolve())


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
    ),
    LaneBackend(
        name=CONDOR_BACKEND,
        executor_factory=_build_condor_executor,
        policy_check_factory=_build_condor_policy_check,
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
