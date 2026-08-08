"""Composition of the provider resilience/readiness collaborators (#6999).

Four objects that only make sense together: the circuit store, the circuit
owner that writes it, the credential probe, and the per-tick sampler that turns
those two into the single launch-eligibility fact planning reads. Assembled
here, in the composition-root package, so control code never has to know how
they fit together — and so the main bootstrap module stays about the shape of
the orchestrator rather than about this one subsystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..control.provider_availability import ProviderAvailabilityPolicy
from ..control.provider_launch_readiness import ProviderLaunchReadinessSampler
from ..control.provider_resilience import ProviderResilienceManager
from ..execution import SQLiteProviderCircuitStore
from ..execution.provider_readiness_probe import CLIProviderReadinessProbe
from ..infra.config import Config
from ..ports import EventSink
from ..ports.provider_resilience import (
    InMemoryProviderCircuitStore,
    ProviderCircuitStore,
)

if TYPE_CHECKING:
    from ..control.label_manager import LabelManager
    from ..ports.command_runner import CommandRunner
    from ..ports.provider_readiness import ProviderReadinessProbe


def build_provider_circuit_store(state_dir: Path) -> ProviderCircuitStore:
    """The durable circuit store for a real orchestrator."""
    return SQLiteProviderCircuitStore(state_dir / "provider_circuit.sqlite")


def build_provider_resilience(
    config: Config,
    events: EventSink,
    store: ProviderCircuitStore | None = None,
) -> ProviderResilienceManager:
    """The sole owner of provider circuit state.

    Defaults to an in-memory store, which is the honest choice for a
    composition with no state directory: nothing to persist, nothing to
    recover.
    """
    return ProviderResilienceManager(
        config.provider_resilience,
        store=store if store is not None else InMemoryProviderCircuitStore(),
        events=events,
    )


def build_provider_readiness_probe(
    command_runner: "CommandRunner",
) -> CLIProviderReadinessProbe:
    """One typed provider-readiness boundary per orchestrator.

    Shared by the per-tick launch sampler, the launch gate and the live-session
    observer, so all three read one probe (and one short-lived result cache)
    rather than each spawning their own.
    """
    return CLIProviderReadinessProbe(command_runner)


def build_provider_launch_sampler(
    config: Config,
    provider_resilience: ProviderResilienceManager | None,
    provider_readiness_probe: "ProviderReadinessProbe",
    label_manager: "LabelManager | None" = None,
) -> ProviderLaunchReadinessSampler | None:
    """The tick's provider-eligibility sampler, or None without a circuit owner.

    This is the seam that turns two application dependencies — the circuit
    owner and the readiness probe — into the one fact planning consumes, so
    planning itself never probes or writes circuit state (#6999 A3).
    """
    if provider_resilience is None:
        return None
    return ProviderLaunchReadinessSampler(
        config=config,
        policy=ProviderAvailabilityPolicy(
            config,
            provider_resilience,
            label_manager,
            readiness_probe=provider_readiness_probe,
        ),
    )


__all__ = [
    "build_provider_circuit_store",
    "build_provider_launch_sampler",
    "build_provider_readiness_probe",
    "build_provider_resilience",
]
