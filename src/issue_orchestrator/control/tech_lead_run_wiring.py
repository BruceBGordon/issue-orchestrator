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
from .tech_lead_run_ownership import RunOwnershipOutcome, RunReconcileStatus

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports import EventSink, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..domain.models import Session
    from .action_applier import ActionResult
    from .tech_lead_run_ownership import TechLeadRunOwnership
    from .actions import (
        Action,
        CreateTechLeadIssueAction,
        DropTechLeadAction,
        QueueTechLeadAction,
    )


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
    ownership: "TechLeadRunOwnership",
    events: "EventSink",
) -> "TechLeadRunCoordinator":
    """Compose the coordinator from the real policy owners.

    One factory so every trigger path — dashboard route, one-shot CLI, reactive
    failure handling, and the periodic/storm health trigger — gets an
    identically-wired coordinator. Blocking classification comes from
    :class:`LabelManager` so the rule is not re-implemented per call site, and
    ``ownership`` is the LONG-LIVED cross-instance run-claim owner: it is
    injected rather than constructed here because a coordinator is built per
    request and must not be able to forget which runs this engine already holds.
    """
    from .label_manager import LabelManager

    label_manager = LabelManager(config)
    return TechLeadRunCoordinator(
        state=state,
        config=config,
        repository_host=repository_host,
        anchor_host=anchor_host,
        ownership=ownership,
        is_blocking_any=label_manager.is_blocking_any,
        events=events,
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

    @property
    def run_ownership(self) -> "TechLeadRunOwnership": ...


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
        ownership=tick.run_ownership,
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


def authors_global_tech_lead_anchor(action: "CreateTechLeadIssueAction") -> bool:
    """True when this creation AUTHORS a whole-repository run's anchor.

    One apply-time owner creates every tech-lead-authored issue — anchors,
    gated proposals, case files — but only an anchor IS a logical run. The
    distinction is read from the action's declared origin and type rather than
    from its labels, so a proposal issue can never be mistaken for a run and
    made to wait on (or hold) whole-repository exclusivity.
    """
    from ..domain.tech_lead_session import TechLeadCreationKind
    from .action_base import ActionType as _ActionType

    return (
        action.action_type is _ActionType.CREATE_TECH_LEAD_ISSUE
        and action.origin.kind is TechLeadCreationKind.AUTHORS_ANCHOR
    )


def create_owned_tech_lead_issue(
    action: "CreateTechLeadIssueAction",
    *,
    ownership: "Optional[TechLeadRunOwnership]",
    create: "Callable[[CreateTechLeadIssueAction], ActionResult]",
) -> "ActionResult":
    """Reserve the whole-repository run, THEN create its anchor (#6994 R2 F3/A3).

    Claiming after the create is a scan-then-create gap wearing a claim's
    clothes: two engines both find no open anchor, both create one, and only
    afterwards does one of them discover it lost — by which point the duplicate
    GitHub issue exists and no claim can un-create it. So reservation, creation
    and compensation are ONE workflow with explicit outcomes, owned here rather
    than assembled by the apply seam.

    The creating owner already DECLARED which variant it authored, so the run
    identity is read from the action rather than re-derived from marker labels
    at this boundary (#6780's rule, reused here).
    """
    from ..domain.tech_lead_run import global_scope_for_flavor
    from .actions import ActionResult

    if not authors_global_tech_lead_anchor(action):
        # A gated proposal or a case file is not a logical run: it holds no
        # whole-repository exclusivity and must not wait on any.
        return create(action)
    if ownership is None:
        return ActionResult.fail(
            action,
            "No tech-lead run ownership wired; refusing to create an anchor that"
            " could not be coordinated across orchestrators",
        )
    scope = global_scope_for_flavor(action.flavor)
    reservation = ownership.claim(scope)
    if not reservation.owned:
        return ActionResult.fail(
            action,
            f"Not creating a {action.flavor.value} anchor: {reservation.detail}",
        )
    result = create(action)
    if not result.success:
        # Compensation: a reserved-but-uncreated run would make every other
        # tech-lead run queue behind a review that does not exist.
        ownership.release(scope.run_key)
    return result


def intake_owned_tech_lead_anchor(
    action: "CreateTechLeadIssueAction",
    issue_number: int,
    tick: TechLeadTickDependencies,
) -> bool:
    """Queue a freshly created anchor this engine already reserved (#6994).

    The reservation happened BEFORE the create
    (:func:`create_owned_tech_lead_issue`), so this is the second half of one
    owner workflow rather than a claim in a post-create callback. Ownership is re-asserted (not re-raced): it is
    idempotent for the engine that holds it, and a hold that vanished between
    the create and here means a peer now owns the whole-repository run — the
    anchor issue stays open for the next discovery, but it is NOT queued here,
    so the two engines cannot both run the review.

    Returns whether the anchor was queued for this engine.
    """
    from ..domain.tech_lead_run import global_scope_for_flavor
    from .health_review_trigger import intake_created_tech_lead_anchor

    scope = global_scope_for_flavor(action.flavor)
    if not tick.run_ownership.claim(scope).owned:
        logger.warning(
            "[TECH_LEAD] Not queueing anchor #%d (%s): another orchestrator owns"
            " run %s",
            issue_number,
            action.flavor.value,
            scope.run_key,
        )
        return False
    intake_created_tech_lead_anchor(
        action,
        issue_number,
        tick.state,
        cast("QueueCacheStore | None", tick.queue_cache_store),
        cast("TechLeadAuthorityStore | None", tick.tech_lead_authority),
    )
    return True


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

    scope = IssueInvestigationScope(action.issue_number)
    PendingSessionQueues(tick.state).remove_tech_lead(action.issue_number)
    # The run no longer exists, so its shared claim must go back immediately:
    # leaving it held would make a peer wait out the whole lease before it could
    # investigate the same subject.
    tick.run_ownership.release(scope.run_key)
    logger.info(
        "[TECH_LEAD] Withdrew queued investigation for #%d before launch: %s",
        action.issue_number,
        action.reason,
    )
    tick.events.publish(
        make_trace_event(
            EventName.TECH_LEAD_RUN_WITHDRAWN,
            {
                "run_key": scope.run_key,
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

    def launch_queued_tech_lead_session(
        self, tech_lead: PendingTechLeadReview
    ) -> "Optional[Session]": ...

    def terminate_tech_lead_session(self, session: "Session") -> object: ...


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
    return tech_lead_run_coordinator(orchestrator).admit(request)


def tech_lead_run_coordinator(
    orchestrator: TechLeadFacadeHost,
) -> "TechLeadRunCoordinator":
    """The facade's identically-wired run coordinator.

    Exposed (rather than inlined into one admit call) because the facade needs
    the SAME owner for two operations: admitting a request, and reconciling run
    ownership each tick. Building it twice from different inputs is how a second
    view of "which runs do we own" would appear.
    """
    deps = orchestrator.deps
    return build_tech_lead_run_coordinator(
        state=orchestrator.state,
        config=orchestrator.config,
        repository_host=deps.repository_host,  # type: ignore[attr-defined]
        anchor_host=orchestrator,
        ownership=deps.run_ownership,  # type: ignore[attr-defined]
        events=deps.events,  # type: ignore[attr-defined]
    )


def reconcile_orchestrator_tech_lead_ownership(
    orchestrator: TechLeadFacadeHost,
) -> None:
    """Renew leases for live tech-lead runs and apply what changed (#6994).

    One call per tick. The ownership owner decides WHAT each run's lease looks
    like; the CONSEQUENCES are applied here, next to that policy, and they are
    deliberately different per status (round 2 F4):

    * ``LOST`` — we held this run and no longer do. A queued run leaves our
      queue (or we would launch work another engine is already doing) and an
      ACTIVE session is TERMINATED: a session that cannot prove ownership is a
      session running concurrently with a peer's conflicting scope, which is the
      exact outcome the shared ledger exists to prevent.
    * ``CONTENDED`` — a peer holds it and we never did (the restart-behind-an-
      unexpired-lease case). The run is RETAINED and retried: the hold will
      lapse or be released, and withdrawing here strands a recovered anchor
      until somebody restarts the engine again.
    * ``UNAVAILABLE`` — the coordination store could not be read. Nothing is
      withdrawn and nothing is stopped, because a transport failure is not
      evidence about ownership.

    Every non-owned run is published as ``TECH_LEAD_RUN_OWNERSHIP_CHANGED`` so
    the retry loop is observable rather than a silent stall.
    """
    reconciliation = tech_lead_run_coordinator(orchestrator).reconcile_ownership()
    for outcome in reconciliation.outcomes:
        if outcome.status is RunReconcileStatus.OWNED:
            continue
        _publish_ownership_change(orchestrator, outcome)
    lost = set(reconciliation.lost)
    if not lost:
        return
    _withdraw_lost_queued_runs(orchestrator, lost)
    _stop_unowned_active_sessions(orchestrator, lost)


def _withdraw_lost_queued_runs(
    orchestrator: TechLeadFacadeHost, lost: set[str]
) -> None:
    from .session_routing import PendingSessionQueues
    from .tech_lead_run_admission import run_key_of_pending

    queues = PendingSessionQueues(orchestrator.state)
    for item in list(orchestrator.state.pending_tech_lead_reviews):
        if run_key_of_pending(item) in lost:
            logger.warning(
                "[TECH_LEAD] Withdrawing queued %s for #%d: another orchestrator"
                " now owns this run",
                item.flavor.value,
                item.issue_number,
            )
            queues.remove_tech_lead(item.issue_number)


def _stop_unowned_active_sessions(
    orchestrator: TechLeadFacadeHost, lost: set[str]
) -> None:
    """Terminate every ACTIVE tech-lead session we can no longer prove we own.

    Withdrawing the queue entry alone was never enough: the run that matters
    most is the one already executing, and leaving it running is what let two
    engines audit the same repository at once.
    """
    from .tech_lead_run_admission import active_tech_lead_sessions, scope_of_session

    for session in active_tech_lead_sessions(
        orchestrator.config, list(orchestrator.state.active_sessions)
    ):
        scope = scope_of_session(session)
        if scope is None or scope.run_key not in lost:
            continue
        logger.warning(
            "[TECH_LEAD] Terminating active %s session for #%d: this engine can"
            " no longer prove it owns run %s",
            session.agent_label,
            session.issue.number,
            scope.run_key,
        )
        orchestrator.terminate_tech_lead_session(session)


def _publish_ownership_change(
    orchestrator: TechLeadFacadeHost, outcome: "RunOwnershipOutcome"
) -> None:
    from ..events import EventName
    from ..ports import make_trace_event

    deps = orchestrator.deps
    events = deps.events  # type: ignore[attr-defined]
    events.publish(
        make_trace_event(
            EventName.TECH_LEAD_RUN_OWNERSHIP_CHANGED,
            {
                "run_key": outcome.run_key,
                "status": outcome.status.value,
                "holder": outcome.holder,
                "detail": outcome.detail,
            },
        )
    )


def orchestrator_launch_tech_lead_run(
    orchestrator: TechLeadFacadeHost, tech_lead: PendingTechLeadReview
) -> "Optional[Session]":
    """Start one queued tech-lead run through the SINGLE launch authority.

    Both launch paths — the in-tick applier (via the session-launcher callback)
    and the one-shot CLI — reach the facade's ``launch_tech_lead_session``, so
    routing that method here is what makes the authority unbypassable rather
    than merely available (round 2 F2 / A2).
    """
    from .label_manager import LabelManager
    from .tech_lead_launch_authority import TechLeadLaunchAuthority

    deps = orchestrator.deps
    return TechLeadLaunchAuthority(
        state=orchestrator.state,
        config=orchestrator.config,
        ownership=deps.run_ownership,  # type: ignore[attr-defined]
        repository_host=deps.repository_host,  # type: ignore[attr-defined]
        is_blocking_any=LabelManager(orchestrator.config).is_blocking_any,
        events=deps.events,  # type: ignore[attr-defined]
        launch=orchestrator.launch_queued_tech_lead_session,
    ).launch(tech_lead)


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
