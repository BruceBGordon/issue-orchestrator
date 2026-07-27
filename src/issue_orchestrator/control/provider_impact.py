"""Issue-scoped provider-impact transitions (issue #5980).

The fleet-scoped ``provider.*`` events emitted by
:class:`~issue_orchestrator.control.provider_resilience.ProviderResilienceManager`
describe *a circuit*, not *an issue*: they carry no ``issue_number``, and
``DefaultTimelineWriter`` drops every event without one. That left the
provider-blocked label as the only signal that an outage stalled a specific
issue — and the label is shed the moment the circuit closes, so the outage
vanished from issue history exactly when an operator would go looking for it.

:class:`ApplyProviderImpactAction` is the owner command for "a provider outage
started / stopped affecting this issue". It owns *both* halves of the
transition — the blocked-label mutation and the durable issue-scoped record —
so no call site can move the label without leaving history behind, and the
record is only written once the label mutation actually applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..events import EventName
from ..ports import make_trace_event
from ..ports.event_sink import TraceEvent
from .actions import (
    Action,
    ActionResult,
    ActionType,
    AddLabelAction,
    RemoveLabelAction,
)


class ProviderImpactTransition(Enum):
    """Which way an issue crossed the provider-availability boundary."""

    BLOCKED = "blocked"
    CLEARED = "cleared"


_EVENT_BY_TRANSITION: dict[ProviderImpactTransition, EventName] = {
    ProviderImpactTransition.BLOCKED: EventName.PROVIDER_ISSUE_BLOCKED,
    ProviderImpactTransition.CLEARED: EventName.PROVIDER_ISSUE_UNBLOCKED,
}


def format_cooldown(seconds: int) -> str:
    """Compact ``"1h 4m"`` / ``"4m 12s"`` / ``"12s"`` retry-window label."""
    seconds = max(0, int(seconds))
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


@dataclass(frozen=True)
class ApplyProviderImpactAction(Action):
    """Move an issue across the provider-availability boundary, and record it.

    ``providers`` are the provider(s) whose circuit caused the transition;
    ``next_retry_at`` / ``cooldown_remaining_seconds`` describe when the
    soonest circuit will next allow a retry (blocked transitions only, and
    absent when the circuit is recovering rather than open).
    """

    issue_number: int = 0
    transition: ProviderImpactTransition = ProviderImpactTransition.BLOCKED
    label: str = ""
    providers: tuple[str, ...] = ()
    next_retry_at: str | None = None
    cooldown_remaining_seconds: int | None = None
    issue_key: str = ""
    action_type: ActionType = field(
        default=ActionType.APPLY_PROVIDER_IMPACT, init=False
    )

    @property
    def provider_list(self) -> str:
        return ", ".join(self.providers)

    def label_action(self) -> Action:
        """The label half of this transition."""
        if self.transition is ProviderImpactTransition.BLOCKED:
            return AddLabelAction(
                issue_number=self.issue_number,
                label=self.label,
                issue_key=self.issue_key,
                reason=self.reason,
                expected=self.expected,
            )
        return RemoveLabelAction(
            issue_number=self.issue_number,
            label=self.label,
            issue_key=self.issue_key,
            reason=self.reason,
            expected=self.expected,
        )

    @property
    def event_name(self) -> EventName:
        return _EVENT_BY_TRANSITION[self.transition]

    def summary(self) -> str:
        """User-facing one-liner for the issue timeline.

        Blocked entries fold the retry window in ("enter" + "retry" text);
        cleared entries say the issue was released.
        """
        providers = self.provider_list or "provider"
        if self.transition is ProviderImpactTransition.CLEARED:
            return (
                f"Provider available again: {providers} — issue released for retry"
            )
        if self.cooldown_remaining_seconds is not None:
            window = format_cooldown(self.cooldown_remaining_seconds)
            return (
                f"Blocked by provider outage: {providers} unavailable — "
                f"next retry in {window}"
            )
        return f"Blocked by provider outage: {providers} unavailable"

    def event_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issue_number": self.issue_number,
            "issue_key": self.issue_key or str(self.issue_number),
            "transition": self.transition.value,
            "providers": list(self.providers),
            "label": self.label,
            # `summary` is what the timeline projection surfaces as the event
            # summary line (see `timeline._summary_from_data`).
            "summary": self.summary(),
        }
        if self.next_retry_at is not None:
            payload["next_retry_at"] = self.next_retry_at
        if self.cooldown_remaining_seconds is not None:
            payload["cooldown_remaining_seconds"] = self.cooldown_remaining_seconds
        return payload


def apply_provider_impact(
    action: ApplyProviderImpactAction,
    *,
    apply_label: Callable[[Action], ActionResult],
    publish: Callable[[TraceEvent], None],
) -> ActionResult:
    """Apply the label transition, then record the issue-scoped impact.

    The record is written only when the label mutation actually changed the
    issue: a failure leaves no misleading history, and a no-op means the issue
    was already on this side of the boundary (which is also what keeps the
    record from being re-emitted on every tick while an outage persists).
    """
    label_result = apply_label(action.label_action())
    if not label_result.success:
        return ActionResult.fail(
            action, label_result.error or "provider blocked-label transition failed"
        )
    if bool(label_result.details.get("no_op")):
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            label=action.label,
            no_op=True,
        )
    publish(make_trace_event(action.event_name, action.event_payload()))
    return ActionResult.ok(
        action,
        issue_number=action.issue_number,
        label=action.label,
        transition=action.transition.value,
    )


__all__ = [
    "ApplyProviderImpactAction",
    "ProviderImpactTransition",
    "apply_provider_impact",
    "format_cooldown",
]
