"""Human-readable, on-change logging of the tech_lead launch decision.

The planner decides every tick whether a queued tech_lead session (a health
review, failure investigation, or batch review) launches — and if not, why
(paused, no reserved slot, provider circuit open, waiting its turn). That
decision used to be emitted only as an ephemeral ``TECH_LEAD_SKIPPED`` event, so
a session that kept being deferred went silent in the per-issue trace log. This
owner writes the decision to the human log at INFO, keyed ``issue=<n>`` so
``issue-orchestrator trace <n>`` surfaces it, and logs only when the decision
*changes* for an issue so a steady state (e.g. "paused") is logged once, not
every tick. Events remain the machine contract; this is the additive "what did I
decide, and why."
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .decision_change_log import DecisionChangeLog
from ..domain.models import PendingTechLeadReview

_MESSAGE = (
    "trace-tech-lead-decision issue=%d flavor=%s decision=%s reason=%s (pending=%d)"
)


def no_slot_reason(
    *,
    workflow_configured: bool,
    reserved_capacity: int | None,
    worker_active_count: int,
    launched_this_tick: int,
    e2e_occupies_slot: bool,
    max_sessions: int,
    tech_lead_max_concurrent: int | None,
    active_tech_lead: int,
) -> str:
    """The TRUE reason a queued tech_lead session got no slot this tick, from
    facts the planner (the budget/priority owner) supplies — so the deferral is
    trustworthy, never a blanket or false "no capacity" (#6892 review F1).
    ``worker_active_count`` is the PRE-tick worker count, so ``launched_this_tick``
    distinguishes a higher-priority launch consuming the last shared slot (where
    active is still 0) from pre-existing saturation."""
    if not workflow_configured:
        return "tech_lead_workflow_unavailable"
    if reserved_capacity is not None:
        return (
            f"reserved_slot_occupied:max_concurrent={tech_lead_max_concurrent},"
            f"active_tech_lead={active_tech_lead}"
        )
    if worker_active_count >= max_sessions:
        return f"worker_slot_occupied:active={worker_active_count},max={max_sessions}"
    if e2e_occupies_slot:
        return f"e2e_occupies_worker_slot:max={max_sessions}"
    if launched_this_tick > 0:
        return (
            f"higher_priority_launched_this_tick:launched={launched_this_tick},"
            f"max={max_sessions}"
        )
    return f"no_worker_capacity:active={worker_active_count},max={max_sessions}"


class TechLeadLaunchLog:
    """On-change log of per-issue tech_lead launch decisions (INFO)."""

    def __init__(self, logger: logging.Logger) -> None:
        self._log = DecisionChangeLog(logger)

    def _note(
        self,
        item: PendingTechLeadReview,
        decision: str,
        reason: str,
        pending: int,
    ) -> None:
        self._log.note(
            item.issue_number,
            f"{decision}:{reason}",
            _MESSAGE,
            item.issue_number,
            item.flavor.value,
            decision,
            reason,
            pending,
        )

    def gate_skip(
        self, pending: Sequence[PendingTechLeadReview], reason: str
    ) -> None:
        """The launch gate rejected the whole queue this tick (its reason)."""
        for item in pending:
            self._note(item, "skip", reason, len(pending))

    def launch_outcomes(
        self,
        pending: Sequence[PendingTechLeadReview],
        launched: Sequence[PendingTechLeadReview],
        provider_skipped: Sequence[PendingTechLeadReview],
        *,
        reserved: bool,
        provider: object,
    ) -> None:
        """Per-item outcome once the gate opened: launched, provider-skipped, or
        deferred because this tick's slot(s) were already spent."""
        launched_ns = {item.issue_number for item in launched}
        provider_ns = {item.issue_number for item in provider_skipped}
        slot = "reserved_slot" if reserved else "worker_slot"
        for item in pending:
            if item.issue_number in launched_ns:
                self._note(item, "launch", slot, len(pending))
            elif item.issue_number in provider_ns:
                self._note(
                    item, "skip", f"provider_circuit_open:{provider}", len(pending)
                )
            else:
                self._note(item, "defer", "no_free_slot", len(pending))

    def defer_all(
        self, pending: Sequence[PendingTechLeadReview], reason: str
    ) -> None:
        """Every queued item is deferred this tick for one shared ``reason``
        (used by the pre-launch exits — paused, or no slot). The caller (the
        budget/priority owner) supplies the true reason so it is never a blanket
        or false 'no capacity'."""
        for item in pending:
            self._note(item, "defer", reason, len(pending))

    def note_suppressed(
        self, item: PendingTechLeadReview, pending: int
    ) -> None:
        """A failure-investigation dropped by the storm-cohort suppression
        filter — its cohort was escalated this tick (the health-review anchor
        covers it, #6780), so it will not launch. Logged so it is not silently
        removed from the launch decision."""
        self._note(item, "defer", "suppressed_cohort_escalated", pending)

    def retain(self, pending: Sequence[PendingTechLeadReview]) -> None:
        """Forget decisions for issues no longer queued, so a re-queue logs fresh."""
        self._log.retain(item.issue_number for item in pending)
