"""Own tech-lead problem-artifact retention across planning cycles.

Failed-session worktrees contain the evidence a queued or active tech-lead
investigation reads. This module owns both consumers of the retention rule:
cleanup fact gathering asks which issues are held, and end-of-tick fact clearing
preserves the matching cleanup requests until the investigation releases them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


def tech_lead_problem_artifact_hold_issue_numbers(
    state: "OrchestratorState",
    config: "Config",
    tech_lead_authority: "TechLeadAuthorityStore | None" = None,
) -> frozenset[int]:
    """Issues whose failed-session run assets must be held from cleanup.

    A failed session records its cleanup in the same pass that records the
    failure, but the tech-lead work that reads those artifacts launches on a
    later tick. The hold is evaluated fresh from every durable/in-memory owner:

    - failures discovered on this tick;
    - queued failure investigations;
    - active tech-lead sessions; and
    - storm cohorts owned by queued or active health-review anchors.

    Once no pending or active work references an issue, re-evaluation releases
    its cleanup without a separate release mutation.
    """
    from ..domain.tech_lead_session import TechLeadSessionFlavor
    from .tech_lead_session_policy import is_tech_lead_session

    if not (config.tech_lead_review_on_failure and config.tech_lead_review_agent):
        return frozenset()
    held = {failure.issue_number for failure in state.discovered_failures}
    referenced_anchors: set[int] = set()
    for item in state.pending_tech_lead_reviews:
        if item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
            held.add(item.issue_number)
        # The in-memory problem_cohort is non-empty only when its ledger write
        # succeeded, so the durable row below is the single cohort authority.
        referenced_anchors.add(item.issue_number)
    for session in state.active_sessions:
        if is_tech_lead_session(
            config.tech_lead_review_agent, session.issue.agent_type
        ):
            held.add(session.issue.number)
            referenced_anchors.add(session.issue.number)
    if tech_lead_authority is not None:
        for anchor, cohort in tech_lead_authority.list_storm_cohorts():
            if anchor in referenced_anchors:
                held.update(problem.issue_number for problem in cohort)
    return frozenset(held)


# Tick-scoped fact buffers: recorded by discovery/completion seams, consumed by
# one planning pass, and cleared only after that plan is applied.
_DISCOVERED_FACT_ATTRS: tuple[str, ...] = (
    "discovered_reviews",
    "discovered_retrospective_reviews",
    "discovered_awaiting_merge_reconciliations",
    "discovered_awaiting_merge_drifts",
    "discovered_awaiting_merge_escalations",
    "discovered_merge_queue_enqueues",
    "discovered_reworks",
    "discovered_escalations",
    "discovered_failures",
    "stuck_sweep_escalations",
    "immediate_cleanups",
)


def clear_discovered_facts(
    state: "OrchestratorState",
    config: "Config",
    tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    *,
    tick_paused: bool,
) -> None:
    """Clear consumed facts while retaining referenced/disposable cleanups.

    A paused tick consumes nothing, so it retains every fact. ``tick_paused``
    must be the snapshot value the planner used, never a fresh read of mutable
    live state; otherwise a mid-tick pause/resume can either drop unconsumed
    failures or replay facts a partial plan already consumed.

    On a running tick, cleanups survive when tech-lead work still references
    their artifacts or when they own a disposable scratch worktree whose prior
    removal failed. All other tick-scoped facts are cleared.
    """
    if tick_paused:
        return
    held = tech_lead_problem_artifact_hold_issue_numbers(
        state, config, tech_lead_authority
    )
    retained = [
        cleanup
        for cleanup in state.immediate_cleanups
        if cleanup.issue_number in held or cleanup.scratch_worktree
    ]
    for attr in _DISCOVERED_FACT_ATTRS:
        getattr(state, attr).clear()
    state.immediate_cleanups.extend(retained)
