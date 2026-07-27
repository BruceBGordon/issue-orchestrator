"""TechLeadWorkflow - tech_lead review launch policy.

This module encapsulates the decision logic for launching tech_lead review
sessions from the pending-tech-lead queue.

The actual batch trigger (deciding WHEN a tech_lead review should be created)
lives in the fact-gathering/planning path:
`fact_gatherer.gather_tech_lead_facts` -> `planner._plan_tech_lead_issue_creation`.

Usage:
    workflow = TechLeadWorkflow(config=config, events=event_sink)
    decision = workflow.should_launch_tech_lead(pending_tech_lead, paused, available_slots=n)
    if decision.should_launch:
        for tech_lead in decision.tech_lead_to_launch:
            # Launch the tech_lead session
"""

from dataclasses import dataclass
from typing import Sequence

from ...infra.config import Config
from ...events import EventName
from ...domain.models import PendingTechLeadReview
from ...ports import EventSink,  make_trace_event
from .decision_base import WorkflowDecision


@dataclass(frozen=True)
class TechLeadDecision(WorkflowDecision[PendingTechLeadReview]):
    """Decision about what tech_lead actions to take.

    This is the output of the workflow's decision logic.
    """

    @property
    def tech_lead_to_launch(self) -> tuple[PendingTechLeadReview, ...]:
        """Alias for items_to_launch for backwards compatibility."""
        return self.items_to_launch


class TechLeadWorkflow:
    """Decides when pending tech_lead reviews should be launched.

    It contains POLICY (what should happen), not MECHANICS.
    """

    def __init__(self, config: Config, events: EventSink):
        """Initialize the workflow.

        Args:
            config: Configuration with tech_lead settings
            events: EventSink for trace events
        """
        self.config = config
        self.events = events

    def is_configured(self) -> bool:
        """Check if tech_lead review is configured."""
        return self.config.tech_lead_review_agent is not None

    def should_launch_tech_lead(
        self,
        pending_tech_lead: Sequence[PendingTechLeadReview],
        paused: bool,
        *,
        available_slots: int,
    ) -> TechLeadDecision:
        """Determine if and which tech_lead reviews should be launched.

        ``available_slots`` is the tech-lead slot budget for this tick, computed
        by the single slot-accounting owner (``worker_budget.tech_lead_slot_
        availability``). The workflow does NOT recompute a second capacity policy
        (#6892 review A2): it slices the queue by ``available_slots`` and reports
        that same number in the ``TECH_LEAD_LAUNCHING`` event, so the machine
        event can never disagree with the actual planned launches.
        """
        if not self.is_configured():
            return TechLeadDecision.skip("No tech_lead_review_agent configured")

        if not pending_tech_lead:
            return TechLeadDecision.skip("No pending tech_lead reviews")

        gate_skip = self._gate_skip_reason(paused, available_slots)
        if gate_skip:
            return TechLeadDecision.skip(gate_skip)

        tech_lead_to_launch = list(pending_tech_lead)[:available_slots]

        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_LAUNCHING,
                {
                    "count": len(tech_lead_to_launch),
                    "capacity": available_slots,
                    "pending": len(pending_tech_lead),
                },
            )
        )

        return TechLeadDecision.launch(tech_lead_to_launch, available_slots)

    def should_create_health_review(
        self,
        paused: bool,
        *,
        available_slots: int,
    ) -> bool:
        """Gate the periodic health-review anchor creation (ADR-0031 §4).

        Same owned paused/capacity gate as launch decisions, over the same
        owner-computed ``available_slots`` (#6892 review A2): when the
        orchestrator is paused or has no slot, the anchor is NOT created —
        due-ness persists, so creation retries once the gate opens — and
        TECH_LEAD_SKIPPED is emitted with the proper reason (#6763).
        """
        if not self.is_configured():
            return False
        return self._gate_skip_reason(paused, available_slots) is None

    def _gate_skip_reason(self, paused: bool, available_slots: int) -> str | None:
        """Owned paused/capacity gate shared by launch and creation decisions.

        Emits TECH_LEAD_SKIPPED with the rejection reason; returns None when
        tech_lead work may proceed. Capacity is NOT derived here — the caller
        passes the owner-computed ``available_slots`` (#6892 review A2), so the
        workflow never holds a second capacity policy.
        """
        if paused:
            self.events.publish(
                make_trace_event(
                    EventName.TECH_LEAD_SKIPPED,
                    {"reason": "orchestrator_paused"},
                )
            )
            return "Orchestrator paused"

        if available_slots <= 0:
            self.events.publish(
                make_trace_event(
                    EventName.TECH_LEAD_SKIPPED,
                    {"reason": "no_capacity", "available": available_slots},
                )
            )
            return f"No tech_lead capacity (available={available_slots})"
        return None
