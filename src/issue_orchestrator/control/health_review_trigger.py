"""Periodic health-review trigger (ADR-0031 §4).

Single owner for the trigger side of the health-review lifecycle:

- **anchor discovery**: the scoped, exhaustive open-anchor scans shared by
  fact gathering and startup recovery (one eligibility rule for both paths),
  plus the marker-scoped dedup lookup (the ``HEALTH_REVIEW_MARKER_LABEL`` is
  the crash-safe dedup key, ADR-0013);
- **due policy**: the interval math against ``state.last_health_review_at``
  AND a board-change gate — the review fires only when the reviewable board
  differs from the fingerprint recorded at the last review, so an idle
  orchestrator never re-walks an unchanged board (the storm trigger is exempt);
- **planning**: the anchor-issue creation action when the review is due, no
  open anchor or pending launch already covers it, and the owned
  paused/capacity gate (``TechLeadWorkflow``) says go — anchor shaping (labels,
  priority title, milestone intent) comes from the ``tech_lead_issue_policy``
  owner, same as batch anchors;
- **intake**: after a successful creation, route the anchor into the pending
  queue through the owning :class:`PendingSessionQueues` operation for the
  variant the marker label declares (batch vs health — #6768 round 3 typed
  intake), and stamp/persist ``state.last_health_review_at`` so neither the
  next tick nor a restart double-fires. A problem-storm anchor additionally
  records its cohort in the durable ledger (``TechLeadAuthorityStore``) BEFORE
  collapsing the superseded per-issue investigations — the collapse is
  earned by persistence, never assumed (#6780);
- **restart reconciliation**: hydrate ``last_health_review_at`` from the
  durable store AND the newest marker-labeled anchor issue — the issues are
  the crash-safe truth (ADR-0013), so a store persist failure can never
  re-fire the review before the interval elapses. A recovered storm anchor
  also rehydrates its cohort from the ledger: labels prove the anchor
  EXISTS, but they cannot carry which problems it owns (#6780).

The anchor issue then rides the existing batch-issue lifecycle: it is picked
up like any tech-lead-agent issue, the launcher derives the HEALTH_REVIEW flavor
from the marker label, and completion closes the anchor when a valid decision
pair lands (see ``tech_lead_session_policy`` / ``tech_lead_completion``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence

from .health_review_body import PERIODIC_HEALTH_REVIEW_BODY, problem_storm_body
from ..domain.tech_lead_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TechLeadCreationOrigin,
    TechLeadSessionFlavor,
)
from .actions import CreateTechLeadIssueAction, SupportsApplyAction
from .board_review_fingerprint import board_review_fingerprint
from .tech_lead_issue_policy import (
    apply_tech_lead_priority_prefix,
    health_review_issue_labels,
    tech_lead_issue_milestone_intent,
)

if TYPE_CHECKING:
    from ..domain.models import (
        DiscoveredFailure,
        OrchestratorState,
        PendingTechLeadReview,
        TechLeadFacts,
    )
    from ..infra.config import Config
    from ..ports import Issue, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .pending_session_queues import TechLeadQueueOutcome
    from .workflows import TechLeadWorkflow


logger = logging.getLogger(__name__)

HEALTH_REVIEW_ISSUE_TITLE = "Health Review — walk the floor"

# Marker-scoped anchor lookups (health-review dedup / last-fired) match at
# most a handful of issues, so a single 100-item page (the GitHub API page
# maximum) is exhaustive for them. The BROAD tech-lead-agent scan instead uses
# the paginated ``TECH_LEAD_PROPOSAL_SCAN_LIMIT`` (#6779 R4), because gated
# proposals share the tech-lead-agent label and could crowd an older anchor past
# a fixed first page (#6763 findings 4 and 7).
_ANCHOR_SCAN_LIMIT = 100

def health_review_interval_minutes(config: "Config") -> int:
    """Effective health-review interval; 0 when disabled or no tech lead agent."""
    if not config.tech_lead_review_agent:
        return 0
    return config.tech_lead.health_review.interval_minutes


@dataclass(frozen=True)
class HealthReviewDecision:
    """The periodic trigger's verdict AND the board it was decided on.

    The two travel together on purpose. The fingerprint that justified firing a
    review is the one that must later be recorded as reviewed — recomputing it
    at stamp time reads a board that has already moved on (the anchor has been
    created and queued by then, which is itself a board change), so the stamp
    would record a transient state the board never returns to and the gate would
    never suppress anything. Deciding and recording from one value makes the
    gate self-consistent by construction.
    """

    due: bool
    # The board as of the decision. "" means "nothing reviewable on the board".
    fingerprint: str


def health_review_decision(
    config: "Config", state: "OrchestratorState", now: float
) -> HealthReviewDecision:
    """Decide whether the periodic review fires, and on which board.

    Gates, in order:

    1. the configured interval has elapsed since the last review (a debounce
       floor); and
    2. the current board fingerprint is non-empty AND differs from the one
       recorded at the last review.

    So a quiet orchestrator does not re-walk an unchanged board, an empty board
    is never walked, and the first review after startup (no recorded
    fingerprint) fires to clear the backlog. The #6780 problem-storm trigger is
    independent of this gate, but still carries this fingerprint so a storm
    review also records the board it walked.

    The fingerprint is computed even when the review is NOT due, because the
    storm trigger fires on exactly those ticks (including at
    ``interval_minutes=0``, where it is the only trigger) and must record the
    board it walked.
    """
    fingerprint = board_review_fingerprint(state, now)
    interval_minutes = health_review_interval_minutes(config)
    if interval_minutes <= 0:
        return HealthReviewDecision(due=False, fingerprint=fingerprint)
    if now - state.last_health_review_at < interval_minutes * 60:
        return HealthReviewDecision(due=False, fingerprint=fingerprint)
    if not fingerprint:
        return HealthReviewDecision(due=False, fingerprint=fingerprint)
    return HealthReviewDecision(
        due=fingerprint != state.last_reviewed_board_fingerprint,
        fingerprint=fingerprint,
    )


def health_review_due(
    config: "Config", state: "OrchestratorState", now: float
) -> bool:
    """True when the periodic review is due AND the board has unreviewed change.

    Thin read of :func:`health_review_decision` for callers that only need the
    verdict. Anything that goes on to CREATE the review must use the decision
    itself, so the fingerprint it fired on is the one recorded as reviewed.
    """
    return health_review_decision(config, state, now).due


def has_health_review_marker(labels: Iterable[str]) -> bool:
    """True when the ADR-0031 §4 marker label is present.

    Casefolded throughout — GitHub label names are case-insensitive, and the
    marker is crash-safe truth (flavor derivation, dedup, recovery routing).
    """
    marker = HEALTH_REVIEW_MARKER_LABEL.casefold()
    return any(name.casefold() == marker for name in labels)


def _scoped_issues(
    issues: Iterable["Issue"], filter_label: Optional[str]
) -> list["Issue"]:
    """Enforce the ``filtering.label`` scope client-side (casefolded).

    The scope label is also part of the server-side query, but adapters and
    fakes may not filter; eligibility is a policy rule and must hold
    regardless of transport behavior.
    """
    folded = (filter_label or "").casefold()
    return [
        issue
        for issue in issues
        if not folded or any(name.casefold() == folded for name in issue.labels)
    ]


def _anchor_query_labels(config: "Config", *extra: str) -> list[str]:
    """Server-side label scope for anchor queries (agent + filter + extras)."""
    if not config.tech_lead_review_agent:
        raise ValueError(
            "tech_lead anchor discovery requires a configured tech_lead_review_agent"
        )
    return [
        value
        for value in (config.tech_lead_review_agent, config.filtering.label, *extra)
        if value
    ]


def discover_open_tech_lead_anchor_issues(
    repository_host: "RepositoryHost", config: "Config"
) -> list["Issue"]:
    """Scoped, exhaustive discovery of open tech_lead anchor issues.

    Single owner for anchor eligibility, shared by fact gathering and startup
    recovery (#6763 finding 7). Both paths must apply the same
    ``filtering.label`` scope — startup queueing an out-of-scope anchor would
    let a run-scoped restart launch another run's health review — and both
    must page the COMPLETE open set: gated proposals share the tech-lead-agent
    label (#6778), so a proposal backlog could otherwise push an older anchor
    (or approved op) past a small window. The exhaustive scan bound is the
    shared ``TECH_LEAD_PROPOSAL_SCAN_LIMIT`` (#6779 R4, paginated), so the SAME
    open scan feeds both anchor classification and proposal reconciliation.

    The scan is AUTHORITATIVE, so it is requested ``exhaustive`` (#6779 R17): a
    later-page HTTP/transport failure or a cap-exhausted read RAISES rather than
    returning a silently partial set. A dropped page could otherwise hide an
    older batch/health anchor (duplicate creation, missed startup recovery) or
    delay an approved op indefinitely, so a failed scan must block this tick's
    planning/recovery — it is never consumed as "no anchors".
    """
    from .tech_lead_proposals import TECH_LEAD_PROPOSAL_SCAN_LIMIT

    issues = repository_host.list_issues(
        labels=_anchor_query_labels(config),
        state="open",
        limit=TECH_LEAD_PROPOSAL_SCAN_LIMIT,
        exhaustive=True,
    )
    return _scoped_issues(issues, config.filtering.label)


def discover_open_health_review_anchor(
    repository_host: "RepositoryHost", config: "Config"
) -> Optional[int]:
    """Marker-scoped open health-anchor lookup (crash-safe dedup, finding 4).

    Scoping the query on the marker label itself makes the lookup exhaustive
    even when the broader tech-lead-agent scan is crowded past its page size.
    Callers invoke this only while a creation decision is actually pending
    (GitHub API discipline — see ``FactGatherer.gather_tech_lead_facts``).
    """
    issues = repository_host.list_issues(
        labels=_anchor_query_labels(config, HEALTH_REVIEW_MARKER_LABEL),
        state="open",
        limit=_ANCHOR_SCAN_LIMIT,
    )
    scoped = [
        issue
        for issue in _scoped_issues(issues, config.filtering.label)
        if has_health_review_marker(issue.labels)
    ]
    return scoped[0].number if scoped else None


def classify_tech_lead_anchor_issues(
    issues: Iterable["Issue"], filter_label: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    """Classify open tech-lead-agent issues into (batch, health_review) anchors.

    One pass over the shared anchor discovery scan — the marker label
    identifies health-review anchors, the historical title match identifies
    batch anchors. Issues outside the active filter-label scope are ignored
    (belt and braces: the discovery owner already enforces scope). All label
    comparisons casefold — GitHub label names are case-insensitive.
    """
    batch: Optional[int] = None
    health: Optional[int] = None
    for issue in _scoped_issues(issues, filter_label):
        if has_health_review_marker(issue.labels):
            health = issue.number if health is None else health
            continue
        if batch is None and is_batch_anchor_title(issue.title):
            batch = issue.number
    return batch, health


def is_batch_anchor_title(title: str) -> bool:
    """True when a title carries the batch tracking issue's signature.

    The one owner of that historical match, so anchor classification and restart
    recovery of a running batch review agree on what a batch anchor looks like.
    """
    return "Batch Review" in title or "Tech Lead Review" in title


def plan_health_review_issue_creation(
    facts: "Optional[TechLeadFacts]",
    pending_tech_lead: Sequence["PendingTechLeadReview"],
    config: "Config",
    *,
    workflow: "TechLeadWorkflow",
    available_slots: int,
    paused: bool,
    storm_problems: Sequence["DiscoveredFailure"] = (),
) -> Optional[CreateTechLeadIssueAction]:
    """Plan the health-review anchor creation when triggered, not duplicated,
    and allowed by the owned paused/capacity gate.

    Two triggers reach here: the elapsed periodic interval, and a problem storm
    (which fires when the interval is NOT due, and is the only trigger at all
    under ``interval_minutes=0``).

    Dedup layers: the open marker-labeled anchor (GitHub, crash-safe), the
    pending-launch queue (covers the window before the label scan refreshes;
    queue items carry a typed flavor, #6768 B5), and
    ``state.last_health_review_at`` (already folded into
    ``facts.health_review_due``). The gate runs last so TECH_LEAD_SKIPPED is
    only emitted when a creation would otherwise happen; due-ness persists
    (no stamp), so creation retries once the gate opens.

    ``facts.existing_health_review_issue`` is the ONLY open-anchor rule, for
    both triggers: the fact gatherer arms its scan on due-ness OR
    :func:`storm_possible`, so a storm-only tick populates the fact too. There
    is deliberately no fallback scan over the runnable issue queue — that queue
    excludes anything belonging to an active session or session history, so an
    anchor that is open and RUNNING is absent from it, and a second rule that
    disagrees with this one on exactly the storm path is how duplicate anchors
    get minted.

    Anchor shaping (labels including the marker, configured priority title,
    milestone intent) comes from the ``tech_lead_issue_policy`` owner — the same
    policy batch anchors get (#6763 finding 5).
    """
    interval_due = bool(facts and facts.health_review_due)
    if not interval_due and not storm_problems:
        return None
    existing_health_review_issue = (
        facts.existing_health_review_issue if facts is not None else None
    )
    if existing_health_review_issue is not None:
        logger.debug(
            "Planner: health-review anchor #%d already open",
            existing_health_review_issue,
        )
        return None
    if any(
        pending.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        for pending in pending_tech_lead
    ):
        logger.debug("Planner: health review already pending launch")
        return None
    if not workflow.should_create_health_review(
        paused=paused, available_slots=available_slots
    ):
        logger.debug("Planner: health review due but gated (paused/at capacity)")
        return None
    # Carry the board this fired on through to the post-creation stamp. Both
    # triggers record it: a storm review walks the board too, so the periodic
    # gate must count it as reviewed. Absent facts leave it "" — "never
    # reviewed", which fails toward reviewing.
    return build_health_review_anchor_action(
        config,
        fingerprint=(
            facts.health_review_fingerprint if facts is not None else ""
        ),
        storm_problems=storm_problems,
    )


def build_health_review_anchor_action(
    config: "Config",
    *,
    fingerprint: str,
    storm_problems: Sequence["DiscoveredFailure"] = (),
    reason: Optional[str] = None,
) -> CreateTechLeadIssueAction:
    """Shape the health-review anchor's create action (single shaping owner).

    Every health-review anchor — periodic (:func:`plan_health_review_issue_creation`,
    after its dedup/gate checks), problem-storm, and on-demand
    (:func:`ensure_on_demand_health_review_anchor`) — is shaped here, so the
    marker label (crash-safe variant truth), body, configured priority-title
    prefix, and milestone intent are byte-for-byte identical across triggers.
    The caller passes the ``fingerprint`` the review is walking so the
    post-creation stamp records the right board; a caller with a distinct
    trigger (the on-demand path) may override the human-readable ``reason``.
    """
    labels = health_review_issue_labels(config)
    title = apply_tech_lead_priority_prefix(config, HEALTH_REVIEW_ISSUE_TITLE)
    # Milestone travels as INTENT; the applier resolves an explicit name at
    # the create-issue execution boundary (#6769 finding 4). Health anchors
    # have no source PRs, so only the explicit strategy can apply.
    milestone = tech_lead_issue_milestone_intent(config, ())
    trigger_reason = reason or (
        f"problem storm: {len(storm_problems)} issues inside settle window"
        if storm_problems
        else "health review interval elapsed"
    )
    logger.info(
        "Creating health-review anchor issue (labels=%s, reason=%s)",
        labels,
        trigger_reason,
    )
    return CreateTechLeadIssueAction(
        title=title,
        body=(
            problem_storm_body(storm_problems)
            if storm_problems
            else PERIODIC_HEALTH_REVIEW_BODY
        ),
        labels=labels,
        pr_count=0,
        milestone=milestone,
        storm_problems=tuple(storm_problems),
        reason=trigger_reason,
        # This owner decides the variant; the marker label in ``labels`` is the
        # crash-safe restatement of the same decision for recovery/intake.
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        health_review_fingerprint=fingerprint,
        # This IS the anchor: no prior issue to reconcile against (#6957 F6).
        origin=TechLeadCreationOrigin.authors_anchor(),
    )


def _queue_anchor_by_marker(
    state: "OrchestratorState",
    issue_number: int,
    title: str,
    labels: Iterable[str],
    *,
    storm_problems: tuple["DiscoveredFailure", ...] = (),
) -> "TechLeadQueueOutcome":
    """Route an orchestrator-created anchor to its variant's owner queue op.

    The marker label declares the variant (labels are the crash-safe truth
    the launcher later re-derives the flavor from), so both creation intake
    and startup recovery pick the matching operation on
    :class:`PendingSessionQueues` instead of overloading batch intake.
    """
    from .pending_session_queues import PendingSessionQueues

    queues = PendingSessionQueues(state)
    if has_health_review_marker(labels):
        storm_issue_numbers = frozenset(
            problem.issue_number for problem in storm_problems
        )
        if storm_issue_numbers:
            queues.remove_failure_investigations(storm_issue_numbers)
        return queues.queue_health_review(
            issue_number,
            title,
            problem_cohort=storm_problems,
        )
    return queues.queue_batch_review(issue_number, title)


def _persist_storm_cohort(
    tech_lead_authority: "Optional[TechLeadAuthorityStore]",
    issue_number: int,
    storm_problems: tuple["DiscoveredFailure", ...],
) -> bool:
    """Record the cohort durably; True when the anchor now owns it (#6780).

    The durable write comes BEFORE the collapse for the same reason the
    planner queues the individual investigations first: collapsing
    retires the per-issue investigations, so it may only happen once the
    cohort is somewhere that outlives this process. In-memory
    ``problem_cohort`` alone cannot carry it — a crash between anchor creation
    and launch recovers the anchor from its label with no cohort at all.

    A store failure is contained rather than raised: the anchor issue ALREADY
    exists on GitHub, so this cannot be reported as an apply failure (same
    rule as :func:`record_health_review_creation`). Returning False keeps the
    individual investigations queued, so the problems are still worked —
    degraded to per-issue tech_lead, never dropped, never silent.
    """
    if not storm_problems or tech_lead_authority is None:
        return False
    try:
        tech_lead_authority.record_storm_cohort(
            anchor_issue_number=issue_number, cohort=storm_problems
        )
    except Exception:
        logger.error(
            "Failed to persist the storm cohort for health-review anchor #%d; "
            "keeping the %d individual failure investigation(s) queued instead "
            "of collapsing them into an anchor that cannot prove its scope",
            issue_number,
            len(storm_problems),
            exc_info=True,
        )
        return False
    return True


def intake_created_tech_lead_anchor(
    action: CreateTechLeadIssueAction,
    issue_number: int,
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    tech_lead_authority: "Optional[TechLeadAuthorityStore]" = None,
) -> "TechLeadQueueOutcome":
    """Route a successfully created tech_lead anchor into the pending queue.

    A storm anchor first records its cohort in the durable ledger, which is
    what earns it the right to collapse the individual investigations; see
    :func:`_persist_storm_cohort`. Health creations additionally stamp/persist
    ``last_health_review_at`` (:func:`record_health_review_creation` keys off
    the marker label).
    """
    persisted = _persist_storm_cohort(
        tech_lead_authority, issue_number, action.storm_problems
    )
    outcome = _queue_anchor_by_marker(
        state,
        issue_number,
        action.title,
        action.labels,
        storm_problems=action.storm_problems if persisted else (),
    )
    record_health_review_creation(action, state, store)
    return outcome


def queue_recovered_tech_lead_anchor(
    state: "OrchestratorState",
    issue: "Issue",
    tech_lead_authority: "Optional[TechLeadAuthorityStore]" = None,
) -> "TechLeadQueueOutcome":
    """Route a recovered open anchor into the pending queue (startup).

    Same marker rule as creation intake: the queued flavor is forwarded
    verbatim to launch (#6768 B5), so recovering a marker-labeled anchor as
    BATCH_REVIEW would relaunch it as a batch audit — manifest prep, batch
    authority, manifest labels on completion. No timestamp stamping: the
    anchor already exists; ``last_health_review_at`` records creation time.

    A storm anchor also recovers its COHORT from the durable ledger (#6780).
    The cohort is the anchor's act-level authority: the queued item
    hands it to launch as a ``TechLeadLaunchScope``, which becomes
    ``TechLeadLaunchAuthority.problem_issue_numbers``. Recovering without it
    (the in-memory queue is gone after a crash, and the issue BODY is mutable
    human documentation, never authority) would launch a health review that
    rejects every proposal for the very issues that triggered it.
    """
    cohort = (
        tech_lead_authority.load_storm_cohort(anchor_issue_number=issue.number)
        if tech_lead_authority is not None
        else None
    )
    if cohort:
        logger.info(
            "Recovered storm cohort for health-review anchor #%d: %d problem "
            "issue(s)",
            issue.number,
            len(cohort),
        )
    return _queue_anchor_by_marker(
        state,
        issue.number,
        issue.title,
        issue.labels,
        storm_problems=cohort or (),
    )


def ensure_on_demand_health_review_anchor(
    *,
    state: "OrchestratorState",
    config: "Config",
    repository_host: "RepositoryHost",
    action_applier: "SupportsApplyAction",
    queue_cache_store: "Optional[QueueCacheStore]",
    tech_lead_authority: "Optional[TechLeadAuthorityStore]",
    now: float,
) -> "Optional[PendingTechLeadReview]":
    """Discover-or-create the health-review anchor and queue it for launch NOW.

    The on-demand counterpart to the periodic/planner path (ADR-0031 §4). It
    forces a review regardless of the interval+fingerprint debounce:
    :func:`health_review_decision` is still consulted, but only for the
    FINGERPRINT it computes — its ``due`` verdict is deliberately ignored, since
    an explicit operator request overrides the timer. Everything else is the
    existing lifecycle, reused verbatim:

    * an already-open anchor (:func:`discover_open_health_review_anchor`) is
      requeued through :func:`queue_recovered_tech_lead_anchor` — the SAME owner
      startup recovery uses, so a storm anchor keeps its durable cohort;
    * otherwise the anchor is shaped by the shared
      :func:`build_health_review_anchor_action`, created through the same apply
      path the tick uses, and routed through :func:`intake_created_tech_lead_anchor`
      (which queues it AND stamps ``last_health_review_at`` + the walked
      fingerprint, so the very next timer tick will not double-fire).

    Returns the queued HEALTH_REVIEW :class:`PendingTechLeadReview` for the driver
    to launch, or ``None`` when no tech lead agent is configured (health review is
    meaningless without one) or anchor creation failed.
    """
    if not config.tech_lead_review_agent:
        logger.warning(
            "On-demand health review requested but no tech_lead_review_agent is "
            "configured; nothing to launch"
        )
        return None
    existing = discover_open_health_review_anchor(repository_host, config)
    if existing is not None:
        issue = repository_host.get_issue(existing)
        if issue is None:
            logger.warning(
                "Open health-review anchor #%d vanished before requeue", existing
            )
            return None
        logger.info("Reusing open health-review anchor #%d on demand", existing)
        queue_recovered_tech_lead_anchor(state, issue, tech_lead_authority)
        anchor_number: Optional[int] = existing
    else:
        anchor_number = _create_on_demand_health_anchor(
            state=state,
            config=config,
            action_applier=action_applier,
            queue_cache_store=queue_cache_store,
            tech_lead_authority=tech_lead_authority,
            now=now,
        )
        if anchor_number is None:
            return None
    return _queued_health_review(state, anchor_number)


def _create_on_demand_health_anchor(
    *,
    state: "OrchestratorState",
    config: "Config",
    action_applier: "SupportsApplyAction",
    queue_cache_store: "Optional[QueueCacheStore]",
    tech_lead_authority: "Optional[TechLeadAuthorityStore]",
    now: float,
) -> Optional[int]:
    """Shape + create + intake a fresh on-demand health-review anchor.

    The fingerprint comes from :func:`health_review_decision` — the SAME
    plumbing the timer path uses to record the board it walked — but its ``due``
    verdict is ignored (the operator forced this run). An empty fingerprint (a
    board with nothing reviewable) records as "never reviewed", which fails
    toward re-reviewing on the next timer tick, never toward silent suppression.
    Creation goes through the tick's real apply path and
    :func:`intake_created_tech_lead_anchor`, so the anchor is queued and the
    fingerprint stamped exactly as a timer-created anchor would be.
    """
    fingerprint = health_review_decision(config, state, now).fingerprint
    action = build_health_review_anchor_action(
        config,
        fingerprint=fingerprint,
        reason="on-demand health review (operator-triggered)",
    )
    result = action_applier.apply(action)
    if not result.success:
        logger.error(
            "On-demand health-review anchor creation failed: %s",
            result.error or "unknown error",
        )
        return None
    issue_number = result.details.get("issue_number")
    if not isinstance(issue_number, int):
        logger.error(
            "On-demand health-review anchor creation returned no issue number"
        )
        return None
    intake_created_tech_lead_anchor(
        action, issue_number, state, queue_cache_store, tech_lead_authority
    )
    return issue_number


def _queued_health_review(
    state: "OrchestratorState", anchor_number: int
) -> "Optional[PendingTechLeadReview]":
    """The queued HEALTH_REVIEW pending item for the anchor, if present.

    Both intake paths append a HEALTH_REVIEW ``PendingTechLeadReview`` to the
    pending queue (or find it already there on a DUPLICATE), so the launch
    driver reads the typed item back rather than reconstructing it.
    """
    return next(
        (
            item
            for item in state.pending_tech_lead_reviews
            if item.issue_number == anchor_number
            and item.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        ),
        None,
    )


def recover_pending_tech_lead_anchors(
    state: "OrchestratorState",
    *,
    repository_host: "RepositoryHost",
    config: "Config",
    session_exists: Callable[[str], bool],
    tech_lead_authority: "TechLeadAuthorityStore | None",
) -> None:
    """Requeue open tech_lead anchors on startup (crash-safe label recovery).

    Gated tech_lead proposals share the tech lead agent label (#6778) but are
    never anchors: gate-labeled issues await operator approval, and
    op-backed issues without the gate label are approved ops the planner
    executes from the fact scan. Requeuing either as a batch anchor would
    launch a tech_lead session on a proposal issue — the same
    ``reconcile_tech_lead_proposals`` owner the fact gatherer uses excludes
    them here, over an EXHAUSTIVE open scan (#6779 R4) so a proposal backlog
    can never hide an anchor. Startup recovery is read-only (#6779 R10):
    leaked/terminal ledger rows are NOT discarded here — the first control
    tick's fact gatherer surfaces them as cleanup candidates and the planner's
    confirm-and-discard action self-heals them.
    Pattern case files (#6781) share the label too and are pure evidence
    ledgers. The same ``split_tech_lead_case_file_issues`` owner the fact gatherer
    uses excludes them here before anchor recovery.
    """
    from .pending_session_queues import TechLeadQueueOutcome
    from .tech_lead_case_files import split_tech_lead_case_file_issues
    from .tech_lead_proposals import reconcile_tech_lead_proposals

    assert config.tech_lead_review_agent is not None  # caller-gated
    # Shared scoped/exhaustive anchor discovery (#6763 finding 7) feeds the
    # proposal reconciliation (#6779 R2/R4): the SAME exhaustive open scan the
    # fact gatherer classifies, so startup applies the same filtering.label
    # eligibility rule and a proposal backlog can never hide an anchor.
    issues = discover_open_tech_lead_anchor_issues(repository_host, config)
    ops = (
        dict(tech_lead_authority.list_ops())
        if tech_lead_authority is not None
        else {}
    )
    reconciled = reconcile_tech_lead_proposals(issues, ops=ops)
    proposal_skipped = len(issues) - len(reconciled.anchor_candidate_issues)
    anchors, case_files = split_tech_lead_case_file_issues(
        reconciled.anchor_candidate_issues
    )
    if proposal_skipped:
        print(
            f"  Skipped {proposal_skipped} gated tech_lead proposal issue(s) (#6778)"
        )
    if case_files:
        print(f"  Skipped {len(case_files)} pattern case file(s) (#6781)")
    for issue in anchors:
        if session_exists(f"issue-{issue.number}"):
            print(f"  tech_lead issue #{issue.number}: Already running")
            continue
        # The ADR-0031 §4 marker label declares the anchor's variant; the
        # owner routes it (#6768 B5: queued flavor reaches launch verbatim)
        # and rehydrates a storm anchor's cohort from the durable ledger
        # (#6780: the recovered anchor must keep its act-level scope).
        outcome = queue_recovered_tech_lead_anchor(state, issue, tech_lead_authority)
        if outcome is TechLeadQueueOutcome.DUPLICATE:
            print(f"  tech_lead issue #{issue.number}: Already queued")
            continue
        print(f"  tech_lead issue #{issue.number}: Queued ({issue.title})")
    if state.pending_tech_lead_reviews:
        print(
            f"  Found {len(state.pending_tech_lead_reviews)} tech_lead review(s)"
            " to process"
        )


def record_health_review_creation(
    action: CreateTechLeadIssueAction,
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    now: Optional[float] = None,
) -> None:
    """Record a successful anchor creation (marker-labeled actions only).

    Stamps the in-memory state (stops the next tick re-firing) and persists
    the marker durably (stops a restart re-firing). Persistence failure is
    logged, not raised: the created issue must not be reported as an apply
    failure, the in-memory stamp plus the open anchor's marker label guard
    the current process, and a restart reconciles the durable value from the
    anchor issue itself (:func:`hydrate_last_health_review_at`) — the
    external side effect and the durable timestamp cannot silently diverge.
    """
    if not has_health_review_marker(action.labels):
        return
    stamped_at = time.time() if now is None else now
    state.last_health_review_at = stamped_at
    # Record the board the trigger DECIDED on, carried verbatim on the action —
    # never a fresh recompute. By now the anchor has been created and queued
    # into state.pending_tech_lead_reviews, which is itself part of the board, so
    # recomputing here would stamp a transient state that only exists between
    # creation and launch and that the board never returns to. The gate would
    # then never match and would re-fire every interval forever — the exact
    # waste this trigger exists to prevent.
    #
    # "" (a storm anchor planned without facts) means "never reviewed", which
    # makes the next due review fire: fail toward reviewing, never toward
    # silent suppression.
    state.last_reviewed_board_fingerprint = action.health_review_fingerprint
    if store is None:
        return
    try:
        store.save_last_health_review_at(stamped_at)
        store.save_last_reviewed_board_fingerprint(
            state.last_reviewed_board_fingerprint
        )
    except Exception:
        logger.warning(
            "Failed to persist health-review markers; a restart reconciles the "
            "interval from the anchor issue and re-reviews on any board change",
            exc_info=True,
        )


def hydrate_last_health_review_at(
    config: "Config",
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    repository_host: "RepositoryHost",
) -> None:
    """Hydrate the last-fired marker at startup, reconciling with anchor truth.

    The store is the fast path, but the anchor issues themselves are the
    crash-safe truth (ADR-0013): if persisting the stamp failed after an
    anchor was created (disk full, SQLite error), the store is BEHIND — once
    that anchor closes, plain store hydration would re-fire the review before
    the interval elapses. Reconcile by deriving the last-fired time from the
    newest marker-labeled anchor in scope (open or closed) and taking the
    newer of the two; the reconciled value is persisted back so the store
    self-heals. Costs one GitHub call, at startup, only when the trigger is
    armed.
    """
    stored = store.load_last_health_review_at() if store is not None else 0.0
    state.last_health_review_at = stored
    # Rehydrate the reviewed-board fingerprint so a restart does not re-walk an
    # unchanged board. There is no anchor-issue truth for it (unlike the
    # timestamp), so a lost value stays "" and the next due review fires — the
    # fail-toward-reviewing side.
    state.last_reviewed_board_fingerprint = (
        store.load_last_reviewed_board_fingerprint() if store is not None else ""
    )
    if health_review_interval_minutes(config) <= 0:
        return
    anchored = most_recent_health_anchor_created_at(repository_host, config)
    if anchored <= stored:
        return
    logger.info(
        "Reconciled last_health_review_at from anchor truth: store=%.2f anchor=%.2f",
        stored,
        anchored,
    )
    state.last_health_review_at = anchored
    if store is None:
        return
    try:
        store.save_last_health_review_at(anchored)
    except Exception:
        logger.warning(
            "Failed to self-heal persisted last_health_review_at; the next "
            "restart will reconcile from the anchor issue again",
            exc_info=True,
        )


def most_recent_health_anchor_created_at(
    repository_host: "RepositoryHost", config: "Config"
) -> float:
    """Created-at epoch of the newest in-scope health anchor (0.0 when none)."""
    issues = repository_host.list_issues(
        labels=_anchor_query_labels(config, HEALTH_REVIEW_MARKER_LABEL),
        state="all",
        limit=_ANCHOR_SCAN_LIMIT,
    )
    scoped = [
        issue
        for issue in _scoped_issues(issues, config.filtering.label)
        if has_health_review_marker(issue.labels)
    ]
    return max((_created_at_epoch(issue) for issue in scoped), default=0.0)


def _created_at_epoch(issue: "Issue") -> float:
    """Parse an issue's ISO-8601 ``created_at`` into an epoch timestamp."""
    raw = issue.created_at
    if raw is None:
        return 0.0
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
