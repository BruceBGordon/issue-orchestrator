"""Detecting work the orchestrator *thinks* it owns but does not.

Two independent staleness questions asked once per tick, before planning:
an issue labelled in-progress with no running session, and an issue holding
a claim that has expired or vanished. Both are pure observation — they read
state and announce what they found, and the planner decides what to do about
it — so they live together here rather than among the tick's sequencing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..events import EventContext, EventName
from ..ports import EventSink, make_trace_event

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)


def _detect_stale_claims(
    issues: list["Issue"],
    active_sessions: list["Session"],
    claim_manager: object | None,
    events: EventSink,
    event_context: EventContext,
    io_claimed_label: str = "io:claimed",
) -> list["Issue"]:
    """Detect issues with stale claims (io:claimed label but no valid claim).

    A claim is considered stale if:
    1. The issue has the io:claimed label
    2. There's no active session for this issue
    3. The claim has expired or doesn't exist

    Args:
        issues: List of issues to check
        active_sessions: Currently active sessions
        claim_manager: ClaimManager for checking claim validity
        events: Event sink for emitting events
        event_context: Event context for enriching events
        io_claimed_label: Resolved io:claimed label string

    Returns:
        List of issues with stale claims
    """
    if not claim_manager:
        return []

    # Build set of issues with active sessions
    active_issue_numbers = {s.issue.number for s in active_sessions}

    stale_claim_issues: list["Issue"] = []

    for issue in issues:
        # Only check issues with io:claimed label
        if io_claimed_label not in issue.labels:
            continue

        # Skip issues with active sessions (claim is valid, session is running)
        if issue.number in active_issue_numbers:
            continue

        # Check if claim is valid via ClaimManager
        if hasattr(claim_manager, 'get_current_claim'):
            from ..domain.claim import ClaimFetchError
            try:
                claim = claim_manager.get_current_claim(issue.number)
            except ClaimFetchError:
                logger.warning(
                    "[STALE-CLAIM] Cannot check claim for issue #%d due to API error - skipping",
                    issue.number,
                )
                continue
            if claim is None or (hasattr(claim, 'is_expired') and claim.is_expired()):
                # Claim is stale
                stale_claim_issues.append(issue)
                logger.info(
                    "[STALE-CLAIM] Issue #%d has io:claimed label but no valid claim",
                    issue.number,
                )
                events.publish(make_trace_event(
                    EventName.CLAIM_STALE_DETECTED,
                    event_context.enrich({
                        "issue_number": issue.number,
                        "labels": list(issue.labels),
                    }),
                ))

    return stale_claim_issues


def detect_stale_in_progress(
    observer: object | None,
    state: "OrchestratorState",
    events: EventSink,
    event_context: EventContext,
) -> list["Issue"]:
    """Detect stale in-progress issues."""
    return _detect_stale_in_progress(observer, state, events, event_context)


def _detect_stale_in_progress(observer: object | None, state: "OrchestratorState", events: EventSink, event_context: EventContext) -> list["Issue"]:
    """Detect stale in-progress issues."""
    if not (observer and hasattr(observer, 'detect_stale_in_progress')):
        return []
    stale_issues = observer.detect_stale_in_progress(state.cached_queue_issues, state.active_sessions)
    for issue in stale_issues:
        events.publish(make_trace_event(EventName.STALE_IN_PROGRESS_DETECTED, event_context.enrich({"issue_number": issue.number, "labels": list(issue.labels)})))
    return stale_issues


__all__ = [
    "_detect_stale_claims",
    "_detect_stale_in_progress",
    "detect_stale_in_progress",
]
