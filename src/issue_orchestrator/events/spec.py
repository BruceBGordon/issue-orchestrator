"""Declarative projection spec — single source of truth for how a
*public* event appears in the timeline.

`EVENT_SPEC` is keyed by `PublicEventName`: only events that surface
in the user or ops timeline view have a projection contract. Private
(debug-only) events are intentionally absent — their projection is an
implementation detail of the legacy fallback in `timeline.py` and not
part of the user-facing timeline contract.

Every `PublicEventName` has exactly one `EventSpec` entry mapping it
to its display fields:

    phase   — high-level stage shown in UI ("in_progress", "reviewing", …)
    step    — the specific action within the phase ("started", "approved")
    status  — outcome marker ("started", "completed", "failed")
    level   — visibility tier ("phase" = top-level, "detail" = nested)

Why a separate `events/spec.py`?

  - `events/catalog.py` (`EventName`, `PublicEventName`) is a stable
    string contract. It rarely changes.
  - `spec.py` is a *behavior* contract — how each public event renders.
    It evolves independently as the timeline UX is refined.

Splitting them prevents UX iteration from churning the wire-protocol
enums and lets the spec own its own test suite.

The projection helpers in `timeline.py` consult `EVENT_SPEC` for
public events; private events fall through to legacy logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import EventName, PublicEventName


@dataclass(frozen=True)
class EventSpec:
    """How a single public event projects into a `TimelineEvent` shape."""

    phase: str
    step: str
    status: str
    level: str


# ---------------------------------------------------------------------------
# EVENT_SPEC — every PublicEventName maps to exactly one EventSpec.
#
# Order is grouped by family (session, review, issue, …) so reviewers
# can scan adjacent rows and spot anomalies (e.g. one review event with
# the wrong status).
# ---------------------------------------------------------------------------

EVENT_SPEC: dict[PublicEventName, EventSpec] = {
    # ----- Session lifecycle -----
    PublicEventName.SESSION_STARTED: EventSpec(phase='in_progress', step='started', status='started', level='detail'),
    PublicEventName.SESSION_COMPLETED: EventSpec(phase='in_progress', step='completed', status='completed', level='detail'),
    PublicEventName.SESSION_FAILED: EventSpec(phase='in_progress', step='failed', status='failed', level='detail'),
    PublicEventName.SESSION_TIMEOUT: EventSpec(phase='in_progress', step='timeout', status='failed', level='detail'),
    PublicEventName.SESSION_BLOCKED: EventSpec(phase='in_progress', step='blocked', status='failed', level='detail'),
    PublicEventName.SESSION_NO_COMPLETION_RECORD: EventSpec(phase='in_progress', step='no_completion_record', status='completed', level='detail'),
    PublicEventName.SESSION_INVALID_COMPLETION_RECORD: EventSpec(phase='in_progress', step='invalid_completion_record', status='failed', level='detail'),
    PublicEventName.SESSION_PROCESSING_COMPLETED: EventSpec(phase='in_progress', step='processing_completed', status='completed', level='detail'),
    PublicEventName.SESSION_VALIDATION_PASSED: EventSpec(phase='orchestrator', step='validation_passed', status='completed', level='detail'),
    PublicEventName.SESSION_VALIDATION_FAILED: EventSpec(phase='orchestrator', step='validation_failed', status='failed', level='detail'),
    PublicEventName.SESSION_VALIDATION_RETRY_NEEDED: EventSpec(phase='orchestrator', step='validation_retry_needed', status='failed', level='detail'),

    # ----- Observation / completion / worktree -----
    PublicEventName.OBSERVATION_COMPLETION_DETECTED: EventSpec(phase='in_progress', step='completion_detected', status='completed', level='detail'),
    PublicEventName.COMPLETION_LOOKUP: EventSpec(phase='in_progress', step='lookup', status='completed', level='detail'),
    PublicEventName.WORKTREE_RESET: EventSpec(phase='system', step='worktree.reset', status='completed', level='detail'),

    # ----- Review workflow -----
    PublicEventName.REVIEW_STARTED: EventSpec(phase='reviewing', step='started', status='started', level='phase'),
    PublicEventName.REVIEW_APPROVED: EventSpec(phase='reviewing', step='approved', status='completed', level='phase'),
    PublicEventName.REVIEW_CHANGES_REQUESTED: EventSpec(phase='reviewing', step='changes_requested', status='failed', level='phase'),
    PublicEventName.REVIEW_ESCALATED: EventSpec(phase='reviewing', step='escalated', status='failed', level='phase'),
    PublicEventName.REVIEW_MERGED: EventSpec(phase='reviewing', step='merged', status='completed', level='phase'),
    PublicEventName.REVIEW_COMMENT_ADDED: EventSpec(phase='reviewing', step='comment_added', status='completed', level='phase'),
    PublicEventName.REVIEW_REWORK_STARTED: EventSpec(phase='reviewing', step='rework_started', status='started', level='phase'),
    PublicEventName.REVIEW_REWORK_COMPLETED: EventSpec(phase='reviewing', step='rework_completed', status='completed', level='phase'),

    # ----- Review exchange (sub-protocol within review) -----
    PublicEventName.REVIEW_EXCHANGE_STARTED: EventSpec(phase='reviewing', step='started', status='started', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_ROUND_STARTED: EventSpec(phase='reviewing', step='round_started', status='completed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_ROUND_COMPLETED: EventSpec(phase='reviewing', step='round_completed', status='completed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_COMPLETED: EventSpec(phase='reviewing', step='completed', status='completed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_FAILED: EventSpec(phase='reviewing', step='failed', status='completed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_ROLE_PROMPTED: EventSpec(phase='reviewing', step='role_prompted', status='completed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_ROLE_FEEDBACK: EventSpec(phase='reviewing', step='role_feedback', status='completed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_ROLE_TIMEOUT: EventSpec(phase='reviewing', step='role_timeout', status='failed', level='phase'),
    PublicEventName.REVIEW_EXCHANGE_CHAPTER_RECORDED: EventSpec(phase='reviewing', step='chapter_recorded', status='completed', level='detail'),

    # ----- Rework cycle -----
    PublicEventName.REWORK_STARTED: EventSpec(phase='rework', step='started', status='started', level='detail'),
    PublicEventName.REWORK_LAUNCHING: EventSpec(phase='rework', step='launching', status='started', level='detail'),

    # ----- Tech Lead -----
    PublicEventName.TECH_LEAD_LAUNCHING: EventSpec(phase='system', step='tech_lead.launching', status='started', level='detail'),

    # ----- Validation -----
    PublicEventName.VALIDATION_STARTED: EventSpec(phase='orchestrator', step='validation.started', status='started', level='detail'),
    PublicEventName.VALIDATION_COMPLETED: EventSpec(phase='orchestrator', step='validation.completed', status='completed', level='detail'),

    # ----- Issue state transitions -----
    PublicEventName.ISSUE_BLOCKED: EventSpec(phase='blocked', step='blocked', status='failed', level='phase'),
    PublicEventName.ISSUE_NEEDS_HUMAN: EventSpec(phase='needs_human', step='needs_human', status='failed', level='phase'),
    PublicEventName.ISSUE_COMPLETED: EventSpec(phase='completed', step='completed', status='completed', level='phase'),
    PublicEventName.ISSUE_UNBLOCKED: EventSpec(phase='in_progress', step='unblocked', status='completed', level='phase'),
    PublicEventName.ISSUE_PR_CREATED: EventSpec(phase='orchestrator', step='pr_created', status='completed', level='phase'),

    # ----- Provider outage impact on this issue (issue #5980) -----
    PublicEventName.PROVIDER_ISSUE_BLOCKED: EventSpec(phase='blocked', step='provider_blocked', status='failed', level='phase'),
    PublicEventName.PROVIDER_ISSUE_UNBLOCKED: EventSpec(phase='in_progress', step='provider_unblocked', status='completed', level='phase'),

    # ----- Publish failure -----
    PublicEventName.PUBLISH_FAILED: EventSpec(phase='orchestrator', step='publish.failed', status='failed', level='detail'),
}


def spec_for(event_name: str | EventName | PublicEventName) -> EventSpec | None:
    """Return the `EventSpec` for a public event, or None.

    Returns None for:
      - Private events (in `EventName` but not in `PublicEventName`)
      - Strings that do not match any catalogued event name

    Callers in the projection layer treat None as "no public spec —
    fall back to legacy logic." This keeps private/debug events out
    of the spec contract while preserving their existing behavior.
    """
    if isinstance(event_name, PublicEventName):
        return EVENT_SPEC.get(event_name)
    if isinstance(event_name, EventName):
        try:
            return EVENT_SPEC[PublicEventName(event_name.value)]
        except (ValueError, KeyError):
            return None
    try:
        return EVENT_SPEC[PublicEventName(event_name)]
    except (ValueError, KeyError):
        return None
