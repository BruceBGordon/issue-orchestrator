"""Wiring the tech-lead run owner into each host that requests a run (#6994).

The admission policy has ONE implementation
(:class:`..control.tech_lead_run_admission.TechLeadRunCoordinator`), but three
very differently-shaped callers reach it: the orchestrator facade (which holds a
dependency container), the in-tick action applier (which does not hold the
facade but does hold every input it needs), and the CLI. Left in the policy
module, that plumbing crowded out the policy; spelled out at each call site, it
would have every entrypoint knowing which six internals a coordinator needs.

So this module owns composition and nothing else: the shared anchor-lifecycle
adapter, one structural protocol per host shape, and the single factory that
wires the real owners — anchor discovery from the health-review trigger,
blocking classification from :class:`LabelManager` — so no call site
re-implements either rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional, Protocol, cast

from ..domain.models import PendingTechLeadReview
from .action_base import ActionType
from ..domain.tech_lead_run import (
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunRequest,
    TechLeadRunTrigger,
)
from .tech_lead_run_admission import (
    SupportsHealthReviewAnchor,
    TechLeadRunCoordinator,
    logger,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports import EventSink, RepositoryHost
    from ..ports.claim_manager import ClaimManager
    from .action_applier import ActionResult
    from .actions import Action, DropTechLeadAction, QueueTechLeadAction


@dataclass(frozen=True, slots=True)
class HealthReviewAnchorLifecycle:
    """:class:`SupportsHealthReviewAnchor` over the raw anchor-lifecycle inputs.

    The orchestrator facade already exposes ``ensure_health_review_anchor`` and
    satisfies the protocol directly. Callers that live INSIDE the tick (the
    action applier) do not hold the facade but do hold every input the shared
    lifecycle owner needs, so this adapter lets them reach the same owner
    instead of growing a second anchor path.
    """

    state: "OrchestratorState"
    config: "Config"
    repository_host: "RepositoryHost"
    action_applier: object
    queue_cache_store: object = None
    tech_lead_authority: object = None
    now: float = 0.0

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]:
        import time as _time

        from .health_review_trigger import ensure_on_demand_health_review_anchor

        return ensure_on_demand_health_review_anchor(
            state=self.state,
            config=self.config,
            repository_host=self.repository_host,
            action_applier=self.action_applier,  # type: ignore[arg-type]
            queue_cache_store=self.queue_cache_store,  # type: ignore[arg-type]
            tech_lead_authority=self.tech_lead_authority,  # type: ignore[arg-type]
            now=self.now or _time.time(),
        )


def build_tech_lead_run_coordinator(
    *,
    state: "OrchestratorState",
    config: "Config",
    repository_host: "RepositoryHost",
    anchor_host: SupportsHealthReviewAnchor,
    events: "EventSink",
    claim_manager: "Optional[ClaimManager]" = None,
    now: Optional[datetime] = None,
) -> "TechLeadRunCoordinator":
    """Compose the coordinator from the real policy owners.

    One factory so every trigger path — dashboard route, one-shot CLI, and the
    in-tick applier — gets an identically-wired coordinator. Anchor discovery
    comes from the health-review trigger owner (the marker-label scan that is
    shared truth) and blocking classification from :class:`LabelManager`, so
    neither rule is re-implemented per call site.
    """
    from .health_review_trigger import discover_open_health_review_anchor
    from .label_manager import LabelManager

    label_manager = LabelManager(config)
    return TechLeadRunCoordinator(
        state=state,
        config=config,
        repository_host=repository_host,
        anchor_host=anchor_host,
        discover_open_anchor=discover_open_health_review_anchor,
        is_blocking_any=label_manager.is_blocking_any,
        events=events,
        claim_manager=claim_manager,
        now=now,
    )


class TechLeadTickDependencies(Protocol):
    """What the apply seam must supply to reach the run-admission owner.

    Named structurally rather than importing ``OrchestratorSupport``: the tick
    already holds every one of these, so declaring the seam as a protocol keeps
    the control owner independent of the apply-time class AND stops the call
    site threading eight loose arguments through — the caller hands over the
    thing it already is.
    """

    @property
    def state(self) -> "OrchestratorState": ...

    @property
    def config(self) -> "Config": ...

    @property
    def repository_host(self) -> "RepositoryHost": ...

    @property
    def events(self) -> "EventSink": ...

    @property
    def action_applier(self) -> object: ...

    @property
    def queue_cache_store(self) -> object: ...

    @property
    def tech_lead_authority(self) -> object: ...


def admit_planned_tech_lead_investigation(
    action: "QueueTechLeadAction", tick: TechLeadTickDependencies
) -> TechLeadRunAdmission:
    """Admit one reactively planned investigation at the apply seam (#6994).

    The in-tick applier does not hold the orchestrator facade, but it holds
    every input the anchor lifecycle needs — so it reaches the SAME admission
    owner the dashboard and CLI use instead of mutating the pending queue
    directly. The planned action already carries its typed failure context, so
    admission spends no extra GitHub read here.
    """
    admission = build_tech_lead_run_coordinator(
        state=tick.state,
        config=tick.config,
        repository_host=tick.repository_host,
        anchor_host=HealthReviewAnchorLifecycle(
            state=tick.state,
            config=tick.config,
            repository_host=tick.repository_host,
            action_applier=tick.action_applier,
            queue_cache_store=tick.queue_cache_store,
            tech_lead_authority=tick.tech_lead_authority,
        ),
        events=tick.events,
    ).admit(
        TechLeadRunRequest(
            scope=IssueInvestigationScope(action.issue_number),
            trigger=TechLeadRunTrigger.AUTOMATIC_FAILURE,
            failure=action.failure,
            title=action.title,
        )
    )
    if not admission.outcome.has_run:
        logger.info(
            "[TECH_LEAD] Reactive investigation for #%d not admitted: %s (%s)",
            action.issue_number,
            admission.outcome.value,
            admission.reason,
        )
    return admission


def withdraw_revalidated_tech_lead_run(
    action: "DropTechLeadAction", tick: TechLeadTickDependencies
) -> None:
    """Remove one queued investigation that launch-time revalidation refused.

    The apply seam owns the mutation, but not the RULE: the planner already
    asked :func:`..control.tech_lead_run_admission.issue_run_eligibility`, and
    the typed refusal it produced rides on the action. Removal goes through
    :class:`PendingSessionQueues`, the single writer for this queue, and the
    withdrawal is published so a run that vanished between queueing and launch
    is machine-readable rather than only a log line.
    """
    from ..events import EventName
    from ..ports import make_trace_event
    from .session_routing import PendingSessionQueues

    PendingSessionQueues(tick.state).remove_tech_lead(action.issue_number)
    logger.info(
        "[TECH_LEAD] Withdrew queued investigation for #%d before launch: %s",
        action.issue_number,
        action.reason,
    )
    tick.events.publish(
        make_trace_event(
            EventName.TECH_LEAD_RUN_WITHDRAWN,
            {
                "run_key": IssueInvestigationScope(action.issue_number).run_key,
                "issue_number": action.issue_number,
                "reason": action.reason,
                "detail": action.detail,
            },
        )
    )


def tech_lead_state_handlers(
    tick: TechLeadTickDependencies,
) -> dict[ActionType, "Callable[[Action, ActionResult], None]"]:
    """Every tech-lead queue transition's apply-seam handler, in one map.

    Mirrors ``tech_lead_action_handlers`` on the applier side: the tick owns
    WHEN a handler runs, this module owns WHAT it does. Handing back a map
    rather than growing one thin delegating method per action on
    ``OrchestratorSupport`` keeps the queue-transition policy beside the owner
    that implements it, so adding a transition does not widen the apply-time
    class that already sits over its line budget.
    """

    def queue(action: "Action", _result: "ActionResult") -> None:
        admit_planned_tech_lead_investigation(cast("QueueTechLeadAction", action), tick)

    def drop(action: "Action", _result: "ActionResult") -> None:
        withdraw_revalidated_tech_lead_run(cast("DropTechLeadAction", action), tick)

    return {
        ActionType.QUEUE_TECH_LEAD: queue,
        ActionType.DROP_TECH_LEAD: drop,
    }


class TechLeadFacadeHost(Protocol):
    """The orchestrator-facade shape the two tech-lead facade operations need.

    Structural, so this control owner never imports the infra facade. Its point
    is to keep the facade's tech-lead methods one-line delegations: the
    dependency plumbing for an anchor lifecycle and a run coordinator lives
    HERE, next to the policy it feeds, instead of being spelled out again at
    every facade method.
    """

    @property
    def state(self) -> "OrchestratorState": ...

    @property
    def config(self) -> "Config": ...

    @property
    def deps(self) -> object: ...

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]: ...


def orchestrator_health_review_anchor(
    orchestrator: TechLeadFacadeHost,
) -> Optional[PendingTechLeadReview]:
    """Discover-or-create the marker-labelled anchor and queue it for launch."""
    return _facade_anchor_lifecycle(orchestrator).ensure_health_review_anchor()


def orchestrator_tech_lead_run(
    orchestrator: TechLeadFacadeHost, request: TechLeadRunRequest
) -> TechLeadRunAdmission:
    """Admit one scoped tech-lead run through the single coordinator (#6994).

    The facade passes ITSELF as the anchor host, so a global admission drives
    the same ``ensure_health_review_anchor`` lifecycle the periodic trigger uses.
    """
    deps = orchestrator.deps
    return build_tech_lead_run_coordinator(
        state=orchestrator.state,
        config=orchestrator.config,
        repository_host=deps.repository_host,  # type: ignore[attr-defined]
        anchor_host=orchestrator,
        events=deps.events,  # type: ignore[attr-defined]
        claim_manager=deps.claim_manager,  # type: ignore[attr-defined]
    ).admit(request)


def _facade_anchor_lifecycle(
    orchestrator: TechLeadFacadeHost,
) -> HealthReviewAnchorLifecycle:
    """Wire the shared anchor lifecycle from the facade's dependency container."""
    deps = orchestrator.deps
    return HealthReviewAnchorLifecycle(
        state=orchestrator.state,
        config=orchestrator.config,
        repository_host=deps.repository_host,  # type: ignore[attr-defined]
        action_applier=deps.action_applier,  # type: ignore[attr-defined]
        queue_cache_store=deps.queue_cache_store,  # type: ignore[attr-defined]
        tech_lead_authority=deps.tech_lead_authority,  # type: ignore[attr-defined]
    )
