"""View registry — maps internal events to external timeline events.

Each internal event (emitted by orchestrator code) fans out to one or more
external events written to the timeline store.  Each external event carries
view tags that control which UI views display it.

Views (ordered by detail level):
    user  — end-user / PM: the story (agent started, reviewed, outcome)
    ops   — operator running the orchestrator: above + validation, retries
    debug — full internal trace: everything

The TimelineWriter consults this registry at write time.  The view model
filters by the requested view before rendering.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ViewEvent:
    """One external event produced from an internal event."""

    name: str
    views: frozenset[str]
    narrative: str | None = None
    phase: str | None = None  # override logical_phase for display

    def visible_in(self, view: str) -> bool:
        return view in self.views


# Shorthand constructors
def _user(name: str, narrative: str, phase: str | None = None) -> ViewEvent:
    """Event visible in user, ops, and debug views."""
    return ViewEvent(name, frozenset({"user", "ops", "debug"}), narrative, phase)


def _ops(name: str, narrative: str | None = None, phase: str | None = None) -> ViewEvent:
    """Event visible in ops and debug views."""
    return ViewEvent(name, frozenset({"ops", "debug"}), narrative, phase)


def _debug(name: str | None = None) -> ViewEvent:
    """Event visible only in debug view. Name defaults to internal name (filled by writer)."""
    return ViewEvent(name or "", frozenset({"debug"}))


# ---------------------------------------------------------------------------
# Registry: internal event name -> list of external ViewEvents
#
# When an internal event has no entry here, a single debug-only record
# is written using the internal event name.
# ---------------------------------------------------------------------------

VIEW_REGISTRY: dict[str, list[ViewEvent]] = {
    # -- Coding agent lifecycle --
    "session.started": [
        _user("agent.coding_started", "Coding agent started", "coding"),
    ],
    "observation.completion_detected": [
        _user("agent.coding_completed", "Agent finished coding", "coding"),
    ],
    "session.completed": [
        _user("agent.completed", "Coding session completed", "orchestrator"),
    ],
    "session.failed": [
        _user("agent.failed", "Agent session failed", "orchestrator"),
    ],
    "session.invalid_completion_record": [
        _ops("agent.invalid_completion_record", "Completion record rejected", "orchestrator"),
    ],
    "session.timeout": [
        _user("agent.timed_out", "Agent timed out", "orchestrator"),
    ],
    "session.blocked": [
        _user("agent.blocked", "Agent blocked", "orchestrator"),
    ],

    # -- Review lifecycle --
    "review.started": [
        _user("review.started", "Code review started", "review"),
    ],
    "review_exchange.started": [
        _user("review_exchange.started", "Review exchange started", "review"),
    ],
    "review_exchange.round_started": [
        _user("review_exchange.round_started", "Review round started", "review"),
    ],
    "review_exchange.round_completed": [
        _user("review_exchange.round_completed", "Review round completed", "review"),
    ],
    "review_exchange.completed": [
        _user("review_exchange.completed", "Review exchange completed", "review"),
    ],
    "review_exchange.failed": [
        _user("review_exchange.failed", "Review exchange failed", "review"),
    ],
    "review_exchange.role_prompted": [
        _user("review_exchange.role_prompted", "Reviewer/coder prompt sent", "review"),
    ],
    "review_exchange.role_feedback": [
        _user("review_exchange.role_feedback", "Reviewer/coder feedback", "review"),
    ],
    "review_exchange.role_timeout": [
        _user("review_exchange.role_timeout", "Reviewer/coder did not complete", "review"),
    ],
    "review_exchange.chapter_recorded": [
        _ops(
            "review_exchange.chapter_recorded",
            "Chapter recorded in role recording",
            "review",
        ),
    ],
    "review.approved": [
        _user("review.approved", "Review approved", "review"),
    ],
    "review.changes_requested": [
        _user("review.changes_requested", "Reviewer requested changes", "review"),
    ],
    "review.escalated": [
        _user("review.escalated", "Escalated to human review", "review"),
    ],
    "review.merged": [
        _user("review.merged", "PR merged", "review"),
    ],
    "review.comment_added": [
        _ops("review.comment_added", "Review comment posted", "review"),
    ],
    "review.rework_started": [
        _user("review.rework_started", "Coder addressing review feedback", "rework"),
    ],
    "review.rework_completed": [
        _user("review.rework_completed", "Coder finished review rework", "rework"),
    ],

    # -- Rework lifecycle --
    "rework.started": [
        _user("agent.rework_started", "Rework agent started", "rework"),
    ],
    "rework.launching": [
        _ops("rework.launching", "Rework session launching", "rework"),
    ],

    # -- Validation --
    "session.validation_passed": [
        _user("validation.passed", "Validation passed", "orchestrator"),
    ],
    "session.validation_failed": [
        _user("validation.failed", "Validation failed", "orchestrator"),
    ],
    "session.validation_retry_needed": [
        _user("validation.retry", "Validation failed — retrying", "orchestrator"),
    ],

    # -- Issue state --
    "issue.pr_created": [
        _user("pr.created", "PR created", "orchestrator"),
    ],
    "issue.blocked": [
        _user("issue.blocked", "Issue blocked", "orchestrator"),
    ],
    "issue.needs_human": [
        _user("issue.needs_human", "Needs human input", "orchestrator"),
    ],
    "issue.completed": [
        _user("issue.completed", "Issue completed", "orchestrator"),
    ],
    "issue.unblocked": [
        _user("issue.unblocked", "Issue unblocked", "orchestrator"),
    ],
    "publish.failed": [
        _user("publish.failed", "Publish failed", "orchestrator"),
    ],

    # -- Provider outage impact on a specific issue (issue #5980) --
    # The fleet-scoped provider.* events carry no issue identity and so can
    # never be stored in an issue timeline. These two are issue-scoped and
    # deliberately user-visible: after the circuit closes and the blocked
    # label is shed, they are the only durable record that a provider outage
    # is why this issue stalled.
    "provider.issue_blocked": [
        _user("provider.issue_blocked", "Blocked by provider outage", "orchestrator"),
    ],
    # Neutral on purpose: a release can mean "the cooldown elapsed, retry
    # allowed, recovery unconfirmed" OR "every circuit reads healthy". The
    # static narrative must not claim recovery the circuit owner never observed
    # (#5980 F4) — the event's `summary` and typed `release_kind` carry the
    # distinction.
    "provider.issue_unblocked": [
        _user("provider.issue_unblocked", "Provider block cleared", "orchestrator"),
    ],
    # User-visible on purpose: this is the story replacing "timed out" when a
    # provider refuses. A reader must be able to tell at a glance that the
    # issue's substance was never the problem — the provider was (#6999). Worded
    # for every refusal the gate can report, not only an expired login.
    "session.launch_blocked_provider": [
        _user(
            "session.launch_blocked_provider",
            "Provider not ready — launch blocked",
            "orchestrator",
        ),
    ],
    # The live-session counterpart. Worded for what actually happened: work was
    # already running and was stopped, which is a different story from a launch
    # that never started (#6999 F5).
    "session.provider_auth_terminated": [
        _user(
            "session.provider_auth_terminated",
            "Provider not authenticated — session stopped",
            "orchestrator",
        ),
    ],
    # A live terminal the orchestrator will not track because it cannot tell
    # what queued work it holds. User-visible: it needs a human (#6999 F6).
    "session.claim_unreadable": [
        _user(
            "session.claim_unreadable",
            "Session quarantined — its queued work could not be identified",
            "orchestrator",
        ),
    ],
    # The other quarantine family: the work IS identified, and is deliberately
    # not being re-queued because a terminal nobody can track is still running
    # it. Worded so a reader does not go and re-queue it by hand (#6999 A1).
    "session.run_unrestorable": [
        _user(
            "session.run_unrestorable",
            "Session quarantined — its run could not be rebuilt",
            "orchestrator",
        ),
    ],

    # -- Tech Lead --
    "tech_lead.launching": [
        _ops("tech_lead.launching", "Tech Lead review launching"),
    ],

    # -- Ops-level events --
    "session.processing_completed": [
        _ops("session.processing_completed", "Session processing completed", "orchestrator"),
    ],
    "session.no_completion_record": [
        _ops("session.no_completion_record", "No completion record found"),
    ],
    "worktree.reset": [
        _ops("worktree.reset", "Worktree reset to main"),
    ],
    "completion.lookup": [
        _ops("completion.lookup", "Completion record lookup"),
    ],
    "validation.started": [
        _ops("validation.started", "Validation started"),
    ],
    "validation.completed": [
        _ops("validation.completed", "Validation completed"),
    ],

    # -- Pure debug (infrastructure noise) --
    "claim.acquired": [_debug()],
    "claim.renewed": [_debug()],
    "claim.released": [_debug()],
    "claim.contested": [_debug()],
    "claim.converged": [_debug()],
    "claim.lost": [_debug()],
    "claim.expired": [_debug()],
    "claim.stale_detected": [_debug()],
    "observation.result": [_debug()],
    "cleanup.completed": [_debug()],
    "issue.labels_changed": [_debug()],
    "issue.claimed": [_debug()],
    "apply.step_applied": [_debug()],
    "apply.started": [_debug()],
    "apply.completed": [_debug()],
    "apply.failed": [_debug()],
    "pr.view_changed": [_debug()],
    "stale.in_progress_detected": [_debug()],
    "stale.in_progress_cleared": [_debug()],
    "session.no_output": [_debug()],
}


def fan_out(internal_event_name: str) -> list[ViewEvent]:
    """Return the external events for an internal event.

    Unregistered events produce a single debug-only record
    with the internal name preserved.
    """
    specs = VIEW_REGISTRY.get(internal_event_name)
    if specs is not None:
        # Fill in empty names (from _debug() shorthand) with internal name
        return [
            ViewEvent(s.name or internal_event_name, s.views, s.narrative, s.phase)
            if not s.name else s
            for s in specs
        ]
    # Default: single debug-only record with internal name
    return [ViewEvent(internal_event_name, frozenset({"debug"}))]


# All valid view names
VIEWS = frozenset({"user", "ops", "debug"})
