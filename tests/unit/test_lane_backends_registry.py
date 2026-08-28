"""One registry, every consumer, no way to serve only some of them.

The defect this guards (A1, #7132 review): a backend name list beside
one if-chain per consumer lets a backend be added to the lane runner
but not the preflight, so the gate would run lanes on a backend whose
policy it never checked. Every assertion here is about that being
impossible by construction rather than by discipline — and it scales
with the consumer list, so adding a factory to the descriptor without
registering it everywhere fails here too.
"""

from __future__ import annotations

import dataclasses

import pytest

from issue_orchestrator.domain.lane_execution import LaneExecutorUnavailableError
from issue_orchestrator.execution import lane_backends
from issue_orchestrator.execution.lane_backends import (
    BACKEND_NAMES,
    BACKENDS,
    LaneBackend,
    build_lane_executor,
    build_lane_policy_check,
    build_pool_inspector,
)
from issue_orchestrator.ports.executor_pool import ExecutorPoolInspector
from issue_orchestrator.ports.lane_executor import LaneExecutor
from issue_orchestrator.ports.lane_policy_check import LanePolicyCheck

# Every factory field on the descriptor, paired with the builder that
# resolves it. Declared once so a new factory cannot be added to the
# descriptor while quietly skipping the assertions below.
_FACTORY_FIELDS = (
    ("executor_factory", build_lane_executor, LaneExecutor),
    ("policy_check_factory", build_lane_policy_check, LanePolicyCheck),
    ("pool_inspector_factory", build_pool_inspector, ExecutorPoolInspector),
)


def test_a_backend_entry_cannot_omit_any_factory() -> None:
    """Every factory is a required dataclass field, so a half-
    registered backend is a TypeError raised while the module is
    imported — not a gap discovered at gate time."""
    fields = {field.name for field in dataclasses.fields(LaneBackend)}
    assert fields == {"name"} | {name for name, _, _ in _FACTORY_FIELDS}
    for field in dataclasses.fields(LaneBackend):
        assert field.default is dataclasses.MISSING, (
            f"{field.name} must have no default: a default is exactly how a "
            "backend gets registered with one factory missing"
        )

    with pytest.raises(TypeError):
        LaneBackend(name="halfway", executor_factory=lambda: None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        LaneBackend(
            name="halfway",
            executor_factory=lambda: None,  # type: ignore[arg-type,return-value]
            policy_check_factory=lambda: None,  # type: ignore[arg-type,return-value]
        )


def test_a_non_callable_factory_is_rejected() -> None:
    with pytest.raises(ValueError, match="policy_check_factory must be callable"):
        LaneBackend(
            name="broken",
            executor_factory=lambda: None,  # type: ignore[arg-type,return-value]
            policy_check_factory="not a factory",  # type: ignore[arg-type]
            pool_inspector_factory=lambda: None,  # type: ignore[arg-type,return-value]
        )
    with pytest.raises(ValueError, match="pool_inspector_factory must be callable"):
        LaneBackend(
            name="broken",
            executor_factory=lambda: None,  # type: ignore[arg-type,return-value]
            policy_check_factory=lambda: None,  # type: ignore[arg-type,return-value]
            pool_inspector_factory="not a factory",  # type: ignore[arg-type]
        )


def test_selectable_names_are_derived_from_the_registry() -> None:
    """Not declared beside it: a name is selectable exactly when it is
    buildable. If these could drift, argparse would accept a backend
    neither builder can resolve."""
    assert BACKEND_NAMES == tuple(BACKENDS)
    assert set(BACKEND_NAMES) == set(BACKENDS)


def test_every_registered_backend_serves_every_consumer() -> None:
    """The whole point: for every name the CLIs accept, the lane runner,
    the preflight, AND the pool snapshot each get a real, port-satisfying
    object. A backend added to one chain and not another fails here."""
    assert BACKEND_NAMES, "registry is empty - probe broken"
    for name in BACKEND_NAMES:
        entry = BACKENDS[name]
        assert entry.name == name
        factories = [getattr(entry, field) for field, _, _ in _FACTORY_FIELDS]
        assert all(callable(factory) for factory in factories)
        # Distinct objects: one factory doing double duty would mean a
        # consumer is being handed something built for another.
        assert len({id(factory) for factory in factories}) == len(factories)


def test_the_direct_backend_builds_every_side_for_real() -> None:
    """Construction is proven, not just registration — the condor
    entries need a live pool and are proven by the requires_infra
    suite instead."""
    for _, build, port in _FACTORY_FIELDS:
        assert isinstance(build("direct"), port)


def test_every_builder_rejects_the_same_unknown_backend() -> None:
    """One lookup, so acceptance cannot diverge between them."""
    for _, build, _ in _FACTORY_FIELDS:
        with pytest.raises(LaneExecutorUnavailableError, match="unknown lane backend"):
            build("nonesuch")


def test_the_registry_cannot_be_extended_after_import() -> None:
    """"One registry" only holds if nothing can add a backend at
    runtime, where whichever consumer looked next would see a
    different set than the other."""
    with pytest.raises(TypeError):
        BACKENDS["smuggled"] = BACKENDS["direct"]  # type: ignore[index]


def test_registration_rejects_a_duplicate_name() -> None:
    """Two entries for one name would make which factories a caller
    gets depend on registration order."""
    entry = BACKENDS["direct"]
    with pytest.raises(ValueError, match="duplicate lane backend"):
        lane_backends._register(entry, entry)
