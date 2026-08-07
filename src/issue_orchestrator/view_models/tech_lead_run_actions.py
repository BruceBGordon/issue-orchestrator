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


class TechLeadRunActionsView(BaseModel):
    """What the dashboard needs to render the two tech-lead actions."""

    # Serialized by alias so the dashboard payload IS this model rather than a
    # hand-built dict: one shape, checked by the public contract, with no
    # untyped seam in between.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    # False when no tech lead agent is configured. The feature stays visible
    # (discoverable) but disabled, with the UI pointing at Settings.
    configured: bool
    # True when the Repository Engine is paused. A paused engine must not claim
    # the action will run, so both actions disable.
    paused: bool
    # Whole-board run status: idle / queued / running.
    global_status: str = Field(serialization_alias="globalStatus")
    # Colour-independent label for the global action ("" when idle).
    global_status_label: str = Field(serialization_alias="globalStatusLabel")
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

    def issue_status(self, issue_number: int) -> str:
        """Status of the targeted action for one issue."""
        if issue_number in self.running_issue_numbers:
            return STATUS_RUNNING
        if issue_number in self.queued_issue_numbers:
            return STATUS_QUEUED
        return STATUS_IDLE

    @classmethod
    def empty(cls) -> "TechLeadRunActionsView":
        """The projection when no engine state is available."""
        return cls(
            configured=False,
            paused=False,
            global_status=STATUS_IDLE,
            global_status_label="",
            queued_issue_numbers=(),
            running_issue_numbers=(),
            global_barrier_active=False,
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

    global_run_numbers = _global_run_issue_numbers(config, state)
    return TechLeadRunActionsView(
        configured=bool(config.tech_lead_review_agent),
        paused=bool(state.paused),
        global_status=global_status,
        global_status_label=_STATUS_LABELS[global_status],
        queued_issue_numbers=tuple(
            sorted(
                item.issue_number for item in pending if not is_global_pending(item)
            )
        ),
        running_issue_numbers=tuple(
            sorted(
                session.issue.number
                for session in active
                if session.issue.number not in global_run_numbers
            )
        ),
        global_barrier_active=global_running or global_queued,
    )


def _global_run_issue_numbers(
    config: "Config", state: "OrchestratorState"
) -> set[int]:
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
    "STATUS_IDLE",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "TechLeadRunActionsView",
    "read_tech_lead_run_actions",
]
