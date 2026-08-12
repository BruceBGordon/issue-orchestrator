"""RetrospectiveReviewWorkflow - review-first audits for existing work."""

import logging
from dataclasses import dataclass
from typing import Sequence

from ...domain.models import PendingRetrospectiveReview
from ...events import EventName
from ...infra.config import Config
from ...ports import EventSink, make_trace_event
from .decision_base import WorkflowDecision

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrospectiveReviewDecision(WorkflowDecision[PendingRetrospectiveReview]):
    """Decision about retrospective review launch work."""

    @property
    def reviews_to_launch(self) -> tuple[PendingRetrospectiveReview, ...]:
        return self.items_to_launch


class RetrospectiveReviewWorkflow:
    """Owns policy for launching review-first existing-implementation audits."""

    def __init__(self, config: Config, events: EventSink) -> None:
        self.config = config
        self.events = events

    def is_configured(self) -> bool:
        return bool(
            self.config.retrospective_review_enabled
            and self.config.code_review_agent
            and self.config.retrospective_review_trigger_label
        )

    def should_launch_reviews(
        self,
        pending_reviews: Sequence[PendingRetrospectiveReview],
        active_session_count: int,
        paused: bool,
    ) -> RetrospectiveReviewDecision:
        if not self.is_configured():
            return RetrospectiveReviewDecision.skip(
                "Retrospective review workflow is not configured"
            )
        if not pending_reviews:
            return RetrospectiveReviewDecision.skip("No retrospective reviews pending")
        if paused:
            self.events.publish(
                make_trace_event(
                    EventName.REVIEW_SKIPPED,
                    {"reason": "retrospective_review_orchestrator_paused"},
                )
            )
            return RetrospectiveReviewDecision.skip("Orchestrator paused")

        available = self.config.max_concurrent_sessions - active_session_count
        if available <= 0:
            self.events.publish(
                make_trace_event(
                    EventName.REVIEW_SKIPPED,
                    {
                        "reason": "retrospective_review_no_capacity",
                        "active": active_session_count,
                        "max": self.config.max_concurrent_sessions,
                    },
                )
            )
            return RetrospectiveReviewDecision.skip("No capacity")

        to_launch = list(pending_reviews)[:available]
        # Log the concrete issue numbers, not just counts: when the queue churns
        # (an issue completes and the freed slot pulls the next one in), this is
        # the single line that answers "which issue just launched, and what's
        # still waiting" without cross-referencing completion/launch timestamps.
        deferred = [r.issue_number for r in list(pending_reviews)[available:]]
        logger.info(
            "Retrospective review launch decision: launching=%s pending=%s capacity=%s "
            "launch_issues=%s deferred_issues=%s",
            len(to_launch),
            len(pending_reviews),
            available,
            [r.issue_number for r in to_launch],
            deferred,
        )
        return RetrospectiveReviewDecision.launch(to_launch, available)
