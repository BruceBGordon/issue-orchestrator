"""Projection of tech-lead run state into the dashboard's action affordances (#6994).

Presentation only. Which runs exist, at what scope, and whether one blocks
another is decided by the run-admission owner
(:mod:`..control.tech_lead_run_admission`); this module only turns that into the
flags and the NON-COLOUR status text the two dashboard actions render.

The affordance is deliberately advisory: a disabled button is a courtesy, never
authority. Every click still goes to ``POST /api/tech-lead/runs``, which
re-decides admission against live state — so a stale view model can at worst
show a stale label, never admit a run it should not have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from ..control.tech_lead_run_admission import (
    active_tech_lead_sessions,
    has_active_global_run,
    is_global_pending,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..domain.tech_lead_session import TechLeadLaunchScope
    from ..infra.config import Config


# Status vocabulary shared by the global and per-issue affordances. Text, not
# colour: the dashboard renders these strings verbatim so the state is legible
# without relying on a tint.
STATUS_IDLE = "idle"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"

_STATUS_LABELS = {
    STATUS_IDLE: "",
    STATUS_QUEUED: "Tech lead queued",
    STATUS_RUNNING: "Tech lead running",
}

# Why the engine as a whole cannot run tech-lead work right now. Resolved HERE,
# in the order the admission owner applies it, so the dashboard renders a
# sentence instead of re-deciding the policy in JavaScript — the UI is an
# adapter, and a second copy of this order in the browser is exactly the drift
# that lets a disabled button contradict the server's rejection.
REASON_ENGINE_STOPPED = (
    "The Repository Engine is not running. Start it to run tech-lead work."
)
REASON_NO_AGENT = "No tech lead agent is configured for this repository."
REASON_DISABLED = (
    "Tech lead is disabled for this repository. Enable it in Settings to run"
    " tech-lead work."
)
REASON_ENGINE_PAUSED = (
    "The Repository Engine is paused. Resume it to run tech-lead work."
)


class TechLeadRunActionsView(BaseModel):
    """What the dashboard needs to render the two tech-lead actions."""

    # Serialized by alias so the dashboard payload IS this model rather than a
    # hand-built dict: one shape, checked by the public contract, with no
    # untyped seam in between.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    # False when no tech lead agent is configured. The feature stays visible
    # (discoverable) but disabled, with the UI pointing at Settings.
    configured: bool
    # False when there is no Repository Engine at all (the dashboard is being
    # served without a live engine). Projected SEPARATELY from ``configured``
    # because the two need different operator remedies — "start the engine" vs
    # "add a tech lead agent in Settings" — and reporting a stopped engine as
    # unconfigured sent operators to the wrong place (#6994 round 1 F5).
    running: bool
    # True when the Repository Engine is paused. A paused engine must not claim
    # the action will run, so both actions disable.
    paused: bool
    # ANY whole-board run's status: idle / queued / running. This is the
    # BARRIER's status, not the health review's — a batch review makes it
    # non-idle too — so it must never be used to decide whether the health
    # action is available (#6994 round 2 F5).
    global_status: str = Field(serialization_alias="globalStatus")
    # Colour-independent label for the barrier ("" when idle).
    global_status_label: str = Field(serialization_alias="globalStatusLabel")
    # The HEALTH REVIEW's own status: idle / queued / running. Projected
    # separately because health and batch reviews are distinct identities that
    # SERIALIZE — a queued batch review must not make the health action look
    # already-requested and refuse the operator's click.
    health_review_status: str = Field(serialization_alias="healthReviewStatus")
    # Colour-independent label for the health action ("" when idle).
    health_review_status_label: str = Field(
        serialization_alias="healthReviewStatusLabel"
    )
    # "" when nothing is in the way; otherwise the sentence explaining that a
    # newly requested run will WAIT (rather than not happen). Only meaningful
    # when the requested scope is not itself the barrier.
    global_barrier_note: str = Field(serialization_alias="globalBarrierNote")
    # Issues with a queued tech-lead investigation.
    queued_issue_numbers: tuple[int, ...] = Field(
        serialization_alias="queuedIssueNumbers"
    )
    # Issues with a running tech-lead investigation.
    running_issue_numbers: tuple[int, ...] = Field(
        serialization_alias="runningIssueNumbers"
    )
    # True when a global run is queued or running, so newly requested targeted
    # work will wait behind it. Surfaced so the UI can say WHY, rather than
    # showing an action that appears to do nothing.
    global_barrier_active: bool = Field(serialization_alias="globalBarrierActive")
    # "" when the engine can run tech-lead work; otherwise the one sentence the
    # dashboard renders for BOTH actions. The engine-level availability policy
    # therefore has exactly one implementation, on this side of the boundary.
    unavailable_reason: str = Field(serialization_alias="unavailableReason")
    # True only when the missing piece is configuration, i.e. when the operator's
    # remedy is Settings. Published rather than inferred from ``configured`` so
    # the UI never has to decide which remedy a state deserves.
    needs_settings: bool = Field(serialization_alias="needsSettings")

    def issue_status(self, issue_number: int) -> str:
        """Status of the targeted action for one issue."""
        if issue_number in self.running_issue_numbers:
            return STATUS_RUNNING
        if issue_number in self.queued_issue_numbers:
            return STATUS_QUEUED
        return STATUS_IDLE

    @property
    def health_review_available(self) -> bool:
        """True when clicking "Run board health review" would achieve something.

        A barrier does NOT make it unavailable: the request would queue behind
        the run in front of it, which is exactly what admission does and what
        the operator asked for. Only an unavailable engine, or a health review
        that already exists, makes the action a no-op.
        """
        return not self.unavailable_reason and self.health_review_status == STATUS_IDLE

    @classmethod
    def empty(cls) -> "TechLeadRunActionsView":
        """The projection when no engine is running.

        ``configured`` is deliberately left True: with no engine we cannot know
        whether a tech lead agent is configured, and claiming it is missing
        would point the operator at Settings for a problem they do not have.
        ``running=False`` is the fact we DO have, and it is the one that
        disables the actions.
        """
        return cls(
            configured=True,
            running=False,
            paused=False,
            global_status=STATUS_IDLE,
            global_status_label="",
            health_review_status=STATUS_IDLE,
            health_review_status_label="",
            global_barrier_note="",
            queued_issue_numbers=(),
            running_issue_numbers=(),
            global_barrier_active=False,
            unavailable_reason=REASON_ENGINE_STOPPED,
            needs_settings=False,
        )


def read_tech_lead_run_actions(
    config: "Config | None", state: "OrchestratorState | None"
) -> TechLeadRunActionsView:
    """Project live tech-lead run state onto the dashboard action affordances.

    Scope classification is delegated to the run-admission owner's helpers, so
    the dashboard can never disagree with the server about what counts as a
    global run.
    """
    if config is None or state is None:
        return TechLeadRunActionsView.empty()

    pending = list(state.pending_tech_lead_reviews)
    active = active_tech_lead_sessions(config, state.active_sessions)
    global_running = has_active_global_run(config, state.active_sessions)
    global_queued = any(is_global_pending(item) for item in pending)

    if global_running:
        global_status = STATUS_RUNNING
    elif global_queued:
        global_status = STATUS_QUEUED
    else:
        global_status = STATUS_IDLE

    health_status = _health_review_status(config, state)
    global_run_numbers = _global_run_issue_numbers(config, state)
    configured = bool(config.tech_lead_review_agent)
    enabled = config.tech_lead_enabled
    explicitly_disabled = config.tech_lead_explicitly_disabled
    paused = bool(state.paused)
    return TechLeadRunActionsView(
        configured=configured,
        running=True,
        paused=paused,
        global_status=global_status,
        global_status_label=_STATUS_LABELS[global_status],
        health_review_status=health_status,
        health_review_status_label=_STATUS_LABELS[health_status],
        global_barrier_note=_barrier_note(global_status, health_status),
        queued_issue_numbers=tuple(
            sorted(item.issue_number for item in pending if not is_global_pending(item))
        ),
        running_issue_numbers=tuple(
            sorted(
                session.issue.number
                for session in active
                if session.issue.number not in global_run_numbers
            )
        ),
        global_barrier_active=global_running or global_queued,
        unavailable_reason=_unavailable_reason(configured, explicitly_disabled, paused),
        needs_settings=not enabled,
    )


def _health_review_status(config: "Config", state: "OrchestratorState") -> str:
    """The HEALTH REVIEW's own status, independent of any other global run.

    Asked per flavor because the admission owner deduplicates per flavor: a
    queued batch review is a barrier the health request waits behind, never a
    reason to refuse it (#6994 round 2 F5).
    """
    for session in active_tech_lead_sessions(config, state.active_sessions):
        if _is_health_review_scope(session.tech_lead_scope):
            return STATUS_RUNNING
    for item in state.pending_tech_lead_reviews:
        if item.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
            return STATUS_QUEUED
    return STATUS_IDLE


def _barrier_note(global_status: str, health_status: str) -> str:
    """What a NEW request would do when a different global run is in front.

    Empty when the health review is itself the barrier — a run is not waiting
    behind itself, and saying so would read as a stall.
    """
    if global_status == STATUS_IDLE or health_status != STATUS_IDLE:
        return ""
    if global_status == STATUS_RUNNING:
        return (
            "Another whole-repository tech-lead review is running; a health"
            " review will start after it."
        )
    return (
        "Another whole-repository tech-lead review is queued; a health review"
        " will start after it."
    )


def _is_health_review_scope(scope: "TechLeadLaunchScope | None") -> bool:
    return scope is not None and scope.flavor is TechLeadSessionFlavor.HEALTH_REVIEW


def _unavailable_reason(
    configured: bool, explicitly_disabled: bool, paused: bool
) -> str:
    """Why neither action can run, in the admission owner's own order."""
    if explicitly_disabled:
        return REASON_DISABLED
    if not configured:
        return REASON_NO_AGENT
    if paused:
        return REASON_ENGINE_PAUSED
    return ""


def _global_run_issue_numbers(config: "Config", state: "OrchestratorState") -> set[int]:
    """Anchor issue numbers currently carrying a whole-board tech-lead run.

    Excluded from the per-issue affordances: a health-review anchor is not a
    board card an operator can aim the targeted action at, so listing it as a
    "running investigation" would attach the state to the wrong surface.
    """
    numbers = {
        item.issue_number
        for item in state.pending_tech_lead_reviews
        if is_global_pending(item)
    }
    for session in active_tech_lead_sessions(config, state.active_sessions):
        scope = session.tech_lead_scope
        if scope is not None and not _is_focus_scope(scope):
            numbers.add(session.issue.number)
    return numbers


def _is_focus_scope(scope: "TechLeadLaunchScope") -> bool:
    return scope.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION


__all__ = [
    "REASON_ENGINE_PAUSED",
    "REASON_ENGINE_STOPPED",
    "REASON_NO_AGENT",
    "STATUS_IDLE",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "TechLeadRunActionsView",
    "read_tech_lead_run_actions",
]
