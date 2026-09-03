"""The declaration seeds and caps; evidence may only lower the request."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.lane_cpu_request import LaneCpuRequest


def test_empty_history_submits_the_declared_seed() -> None:
    """The naive run is byte-for-byte the pre-learning behavior: with
    nothing measured, the lanes.yaml value is what crosses the port."""
    request = LaneCpuRequest.resolve(declared_cpus=8, learned_busy_cores=None)
    assert request.request_cpus == 8
    assert request.declared_cpus == 8
    assert request.learned_busy_cores is None
    assert request.is_capped is False


def test_evidence_below_the_declaration_lowers_the_request() -> None:
    """A lane that measures under its declaration gives capacity back:
    2 declared, 0.85 measured, 1 requested."""
    request = LaneCpuRequest.resolve(declared_cpus=2, learned_busy_cores=0.85)
    assert request.request_cpus == 1


def test_evidence_rounds_up_never_down() -> None:
    """A lane measuring 6.6 cores still needs seven whole cores to run
    on; flooring would under-admit every fractional lane."""
    assert LaneCpuRequest.resolve(7, 6.6).request_cpus == 7
    assert LaneCpuRequest.resolve(7, 6.1).request_cpus == 7
    assert LaneCpuRequest.resolve(7, 5.0).request_cpus == 5


def test_evidence_above_the_declaration_never_raises_the_request() -> None:
    """The whole safety property: a lane suddenly measuring sixteen
    cores is far likelier to be a broken measurement than a lane that
    got eight times hungrier, and granting it would drain the pool.
    The divergence is recorded, not granted."""
    request = LaneCpuRequest.resolve(declared_cpus=2, learned_busy_cores=16.0)
    assert request.request_cpus == 2
    assert request.learned_busy_cores == 16.0
    assert request.is_capped is True


def test_a_lane_always_gets_at_least_one_core() -> None:
    """A floor of zero is not a smaller request, it is an
    unschedulable one — including for a lane measured at exactly 0.0
    (a provider-wait lane that burns no CPU at all)."""
    assert LaneCpuRequest.resolve(4, 0.0).request_cpus == 1
    assert LaneCpuRequest.resolve(4, 0.01).request_cpus == 1


def test_evidence_equal_to_the_declaration_is_not_a_divergence() -> None:
    request = LaneCpuRequest.resolve(declared_cpus=8, learned_busy_cores=8.0)
    assert request.request_cpus == 8
    assert request.is_capped is False


def test_construction_rejects_a_request_above_the_declaration() -> None:
    """The invariant is enforced at the type, not only in resolve(): no
    other construction path may hand out more than the ceiling."""
    with pytest.raises(ValueError, match="may never exceed declared_cpus"):
        LaneCpuRequest(declared_cpus=2, learned_busy_cores=None, request_cpus=3)


def test_construction_rejects_nonsense_measurements() -> None:
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            LaneCpuRequest(
                declared_cpus=4, learned_busy_cores=bad, request_cpus=4
            )
    with pytest.raises(ValueError):
        # The annotation's numeric tower accepts an int; the runtime
        # guard does not — measured busy cores are always floats.
        LaneCpuRequest(
            declared_cpus=4,
            learned_busy_cores=2,  # type: ignore[arg-type]
            request_cpus=4,
        )


def test_construction_rejects_nonsense_declarations() -> None:
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            LaneCpuRequest.resolve(declared_cpus=bad, learned_busy_cores=None)
