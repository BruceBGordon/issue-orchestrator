"""Cross-cutting infrastructure services bundle.

Groups services that many control-layer components need (label management,
persistence, provider resilience, timeline) into a single frozen dataclass.
This replaces 7 individual fields on ``OrchestratorDeps``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..ports.provider_readiness import (
    NO_PROVIDER_READINESS_PROBE,
    ProviderReadinessProbe,
)

if TYPE_CHECKING:
    from ..ports.label_store import LabelStore
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.goal_pilot_store import GoalPilotStore
    from ..ports.attempt_store import AttemptStore
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from ..ports.turn_mailbox import TurnMailbox
    from ..ports.timeline_reader import TimelineReader
    from ..ports.timeline_store import TimelineStore
    from ..ports.timeline_writer import TimelineWriter
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .open_issue_corpus import OpenIssueCorpusManager
    from .background_job_supervisor import BackgroundJobSupervisor
    from .label_manager import LabelManager
    from .provider_launch_readiness import ProviderLaunchReadinessSampler
    from .provider_resilience import ProviderResilienceManager


def _noop_health_check() -> None:
    """Default no-op health check for tests and disabled configurations."""


@dataclass(frozen=True)
class InfraServices:
    """Cross-cutting infrastructure services.

    Bundled into a single object so ``OrchestratorDeps`` doesn't keep growing
    one field at a time.  Backward-compat properties on OrchestratorDeps
    delegate here.
    """

    label_manager: "LabelManager"
    label_store: "LabelStore"
    queue_cache_store: "QueueCacheStore"
    provider_resilience: "ProviderResilienceManager"
    timeline_reader: "TimelineReader"
    timeline_store: "TimelineStore"
    timeline_writer: "TimelineWriter"
    goal_pilot_store: "GoalPilotStore"
    attempt_store: "AttemptStore"
    # Orchestrator-owned tech_lead launch authority port (ADR-0031 / #6769 F2).
    tech_lead_authority: "TechLeadAuthorityStore"
    # Rebuildable GitHub open-issue corpus owner (#6881).
    open_issue_corpus: "OpenIssueCorpusManager"
    # The typed provider-readiness/auth-failure boundary (#6999). Shared by the
    # launch gate and the live-session observer so both consume one probe (and
    # one short-lived result cache) rather than each spawning their own.
    provider_readiness_probe: ProviderReadinessProbe = NO_PROVIDER_READINESS_PROBE
    # Samples provider launch eligibility once per tick, before planning
    # (#6999 A3). None means "no sampler wired", which blocks nothing — a
    # production tick always has one.
    provider_launch_sampler: "ProviderLaunchReadinessSampler | None" = None
    # Cross-repo filing seam for the finding-promotion lane (#6957). None when
    # the repository host is not a real GitHub adapter (offline/testing).
    promotion_target: "PromotionTargetHost | None" = None
    pair_registry: "PersistentExchangePairRegistry | None" = None
    turn_mailbox: "TurnMailbox | None" = None
    background_job_supervisor: "BackgroundJobSupervisor | None" = None
    instance_id: str = ""
    state_health_check: Callable[[], None] = field(default=_noop_health_check)
