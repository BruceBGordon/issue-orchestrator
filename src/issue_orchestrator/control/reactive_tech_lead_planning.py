"""Reactive tech_lead planning: the tech-lead reaction as actions (#6780).

Owner for the ONE atomic decision a tick makes about discovered problems:
escalate a time-bounded cohort to a single unscheduled health-review anchor,
or queue the individual failure investigations — and, either way, whether the
already-queued members may launch this tick.

The split of responsibilities around this module:

- ``tech_lead_reaction.TechLeadReactionPolicy`` CLASSIFIES (which problems are
  tech-lead-worthy, which form a storm). It touches no queues and decides no
  suppression.
- this module MAPS that reaction onto persist-first actions, and owns the
  suppression rule.
- ``health_review_trigger`` owns anchor creation policy and the intake that
  COLLAPSES a cohort once it is durably persisted.

It lives outside ``planner.py`` because the reaction is a policy in its own
right — the planner's job is to order and assemble a tick's actions, not to
host the reaction model's rules (which is also what keeps the planner's
oversized-hotspot budget from absorbing every new reaction rule).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .actions import Action, CreateTechLeadIssueAction, QueueTechLeadAction
from .health_review_trigger import plan_health_review_issue_creation

if TYPE_CHECKING:
    from ..domain.models import DiscoveredFailure
    from ..infra.config import Config
    from .planner_types import OrchestratorSnapshot
    from .tech_lead_reaction import TechLeadReaction
    from .workflows import TechLeadWorkflow

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReactiveTechLeadPlan:
    """Outcome of the tech-lead reaction for one tick (#6780).

    ``actions`` ALWAYS queues the individual investigations the classifier
    selected, and appends the storm/periodic health-review anchor when one can
    be created. Queue-then-collapse is deliberate: the pending queue is the
    only CROSS-TICK carrier of a problem once ``discovered_failures`` is
    cleared at end of tick (it is in-memory only — failure investigations have
    no GitHub anchor and are not recovered on restart — so it outlives the
    tick, not the process). The cohort is therefore persisted FIRST, and a
    successfully created anchor collapses it at intake
    (``_queue_anchor_by_marker`` removes the superseded investigations and
    stamps ``problem_cohort`` onto the pending health review in one step).
    Planning the anchor *instead of* the investigations would lose the cohort
    whenever the create never lands — a GitHub failure or the tech_lead cooldown,
    both invisible to the planner.

    ``suppressed_issue_numbers`` governs LAUNCH timing only, never retention:
    it holds back already-queued member investigations on the tick the cohort
    is escalated, since intake is about to collapse them. If the anchor create
    then fails, those items simply remain queued and launch on a later tick.
    """

    actions: tuple[Action, ...]
    suppressed_issue_numbers: frozenset[int]


def plan_reactive_tech_lead(
    snapshot: "OrchestratorSnapshot",
    reaction: "TechLeadReaction",
    config: "Config",
    *,
    workflow: "Optional[TechLeadWorkflow]",
) -> ReactiveTechLeadPlan:
    """Map the tech-lead reaction onto persist-first actions (#6780).

    The individual investigations are queued unconditionally — they are the
    cross-tick carrier of each problem once the tick-scoped
    ``discovered_failures`` buffer is cleared. The storm anchor is appended
    AFTER them, so that on a successful create the intake owner
    (``_queue_anchor_by_marker``) collapses the cohort atomically: it removes
    the superseded investigations and stamps ``problem_cohort`` onto the
    pending health review, from which the launch authority's
    ``problem_issue_numbers`` later derive.

    Suppression is therefore never the thing that decides retention: every
    path that leaves the cohort without an anchor — an existing or pending
    health review, no capacity, paused, a failed GitHub create, or the
    apply-time tech_lead cooldown — leaves the individual investigations queued,
    and they self-heal into one consolidated health review on a later tick
    once an anchor can be created. Only the intake owner, which alone knows
    the cohort was persisted, retires them.
    """
    health_review_action = plan_health_review_creation(
        snapshot, config, workflow=workflow, storm_problems=reaction.storm_problems
    )
    actions = plan_failure_investigations(reaction.investigations)
    if health_review_action is not None:
        actions.append(health_review_action)
    # Hold back already-queued member launches only on the tick the cohort
    # is actually escalated; intake is about to collapse them into the
    # anchor. A deferred storm suppresses nothing.
    suppressed = (
        reaction.storm_issue_numbers
        if reaction.storm_problems and health_review_action is not None
        else frozenset()
    )
    return ReactiveTechLeadPlan(
        actions=tuple(actions), suppressed_issue_numbers=suppressed
    )


def plan_health_review_creation(
    snapshot: "OrchestratorSnapshot",
    config: "Config",
    *,
    workflow: "Optional[TechLeadWorkflow]",
    storm_problems: tuple["DiscoveredFailure", ...] = (),
) -> Optional[CreateTechLeadIssueAction]:
    """Plan the periodic/storm health-review anchor creation (ADR-0031 §4).

    Policy lives in health_review_trigger; the TechLeadWorkflow owns the
    paused/capacity gate and its TECH_LEAD_SKIPPED emissions (#6763).
    """
    if not workflow:
        return None
    # Owner-computed tech-lead slot budget (worker_budget) — the workflow no
    # longer derives its own capacity (#6892 review A2). Creation runs before
    # this tick's launches, so launched_this_tick is 0.
    from .worker_budget import tech_lead_slot_availability

    available_slots = tech_lead_slot_availability(
        config,
        snapshot.active_sessions,
        e2e_occupies_slot=snapshot.e2e_occupies_slot,
        launched_this_tick=0,
        workflow_configured=workflow.is_configured(),
    ).available
    return plan_health_review_issue_creation(
        snapshot.tech_lead_facts,
        snapshot.pending_tech_lead,
        config,
        workflow=workflow,
        available_slots=available_slots,
        paused=snapshot.paused,
        storm_problems=storm_problems,
    )


def plan_failure_investigations(
    failures: tuple["DiscoveredFailure", ...],
) -> list[Action]:
    """Queue one focused tech_lead investigation per discovered failure.

    The classifier (``TechLeadReactionPolicy``) already decided which failures
    warrant an individual investigation (config gate, dependency explanation,
    dedup against the pending queue); this maps each survivor to a
    ``QueueTechLeadAction``. Called either directly (no storm) or as the storm
    fallback when the cohort could not be escalated (#6780).
    """
    actions: list[Action] = []
    for failure in failures:
        actions.append(QueueTechLeadAction(
            issue_number=failure.issue_number,
            title=f"Investigate: {failure.issue_title} ({failure.failure_reason})",
            # Preserve the typed failure context across the queue boundary:
            # discovered_failures is cleared after planning, but the queued
            # investigation launches on a later tick and its board snapshot
            # must still contain its own triggering failure.
            failure=failure,
            reason=f"Session failed with status '{failure.failure_reason}'",
        ))
        logger.info("Planner: queuing tech_lead for failed issue #%d (%s)",
                   failure.issue_number, failure.failure_reason)
    return actions
