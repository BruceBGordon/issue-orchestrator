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

Everything the command says about providers comes from a single
:class:`ProviderImpactAssessment`: one bounded, point-in-time read of every
provider the issue depends on, taken at one instant. The label decision, the
retry metadata, and the user-facing history text are all derived from that same
assessment, so they cannot drift apart or describe different moments (#5980
F4/A2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable

from ..events import EventName
from ..ports import make_trace_event
from ..ports.event_sink import TraceEvent
from ..ports.provider_resilience import ProviderCircuitStatus
from .actions import (
    Action,
    ActionResult,
    ActionType,
    AddLabelAction,
    RemoveLabelAction,
)


class ProviderAvailability(Enum):
    """How one provider's circuit reads at a single instant.

    Mirrors the three states the circuit owner can be in, and the same three
    the health panel renders:

    ``OPEN``        the circuit is open right now — calls are blocked.
    ``RECOVERING``  tracked, but the cooldown has elapsed; a retry is allowed
                    and no call has succeeded yet, so recovery is unconfirmed.
    ``HEALTHY``     untracked — either it never failed, or ``record_success``
                    confirmed recovery and deleted the row.
    """

    OPEN = "open"
    RECOVERING = "recovering"
    HEALTHY = "healthy"


class ProviderReleaseKind(Enum):
    """Why an issue was released from the provider-blocked state.

    ``COOLDOWN_ELAPSED`` is deliberately distinct from ``AVAILABLE``: a circuit
    that ``close_expired()`` moved out of the open state has *not* been proven
    healthy, so the history must not claim the provider recovered (#5980 F4).
    """

    COOLDOWN_ELAPSED = "cooldown_elapsed"
    AVAILABLE = "available"


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
class ProviderImpactAssessment:
    """One point-in-time read of every provider an issue depends on.

    Built by :class:`ProviderAvailabilityPolicy` from a single ``assessed_at``
    instant, so "is this issue blocked", "which providers are actually to
    blame", and "when is the next retry" are all answered from the same moment.
    Providers are partitioned — a provider appears in exactly one bucket.
    """

    assessed_at: datetime
    open_providers: tuple[str, ...] = ()
    recovering_providers: tuple[str, ...] = ()
    healthy_providers: tuple[str, ...] = ()
    # Soonest instant an *open* circuit next allows a retry, and how far away
    # that is. Both ``None`` when nothing is open (nothing to wait for).
    next_retry_at: str | None = None
    cooldown_remaining_seconds: int | None = None

    @classmethod
    def from_statuses(
        cls,
        assessed_at: datetime,
        statuses: Iterable[tuple[str, ProviderCircuitStatus | None]],
    ) -> "ProviderImpactAssessment":
        """Partition ``(provider, status)`` reads taken at ``assessed_at``.

        ``status is None`` means the circuit owner is not tracking the provider
        at all: it never failed, or a successful call deleted the row.
        """
        open_providers: list[str] = []
        recovering: list[str] = []
        healthy: list[str] = []
        open_statuses: list[ProviderCircuitStatus] = []
        for provider, status in statuses:
            if status is None:
                healthy.append(provider)
            elif status.is_open:
                open_providers.append(provider)
                open_statuses.append(status)
            else:
                recovering.append(provider)
        next_retry_at: str | None = None
        cooldown: int | None = None
        if open_statuses:
            soonest = min(open_statuses, key=lambda s: s.cooldown_remaining_seconds)
            next_retry_at = (
                soonest.open_until.isoformat() if soonest.open_until is not None else None
            )
            cooldown = soonest.cooldown_remaining_seconds
        return cls(
            assessed_at=assessed_at,
            open_providers=tuple(sorted(open_providers)),
            recovering_providers=tuple(sorted(recovering)),
            healthy_providers=tuple(sorted(healthy)),
            next_retry_at=next_retry_at,
            cooldown_remaining_seconds=cooldown,
        )

    @property
    def blocked(self) -> bool:
        """Whether any circuit is open right now — the label decision."""
        return bool(self.open_providers)

    @property
    def release_kind(self) -> ProviderReleaseKind:
        """Why a non-blocked issue is being released.

        A still-tracked ``RECOVERING`` provider means the cooldown merely
        elapsed; nothing has proven the provider healthy yet.
        """
        if self.recovering_providers:
            return ProviderReleaseKind.COOLDOWN_ELAPSED
        return ProviderReleaseKind.AVAILABLE

    def availability(self, provider: str) -> ProviderAvailability:
        if provider in self.open_providers:
            return ProviderAvailability.OPEN
        if provider in self.recovering_providers:
            return ProviderAvailability.RECOVERING
        return ProviderAvailability.HEALTHY

    @property
    def assessed_providers(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                (*self.open_providers, *self.recovering_providers, *self.healthy_providers)
            )
        )

    def as_payload(self) -> dict[str, Any]:
        """Machine-readable partition for the issue-scoped event."""
        return {
            "assessed_at": self.assessed_at.isoformat(),
            "open_providers": list(self.open_providers),
            "recovering_providers": list(self.recovering_providers),
            "healthy_providers": list(self.healthy_providers),
        }


# Placeholder for the dataclass-inheritance default every Action subclass needs.
# Never a usable value: `ApplyProviderImpactAction.__post_init__` rejects any
# action whose assessment does not support its transition, so an action built
# without a real assessment fails loudly instead of recording an empty story.
_UNASSESSED = ProviderImpactAssessment(assessed_at=datetime.min)


@dataclass(frozen=True)
class ApplyProviderImpactAction(Action):
    """Move an issue across the provider-availability boundary, and record it.

    Everything the record says is derived from ``assessment`` — a single
    point-in-time read — so the label decision, the retry window, and the
    history text describe the same moment and the same providers.
    """

    issue_number: int = 0
    transition: ProviderImpactTransition = ProviderImpactTransition.BLOCKED
    label: str = ""
    assessment: ProviderImpactAssessment = _UNASSESSED
    issue_key: str = ""
    action_type: ActionType = field(
        default=ActionType.APPLY_PROVIDER_IMPACT, init=False
    )

    def __post_init__(self) -> None:
        """Fail fast when the assessment cannot support the transition."""
        if self.transition is ProviderImpactTransition.BLOCKED:
            if not self.assessment.open_providers:
                raise ValueError(
                    "provider-impact BLOCKED requires at least one open circuit; "
                    f"assessment has none (issue #{self.issue_number})"
                )
            return
        if self.assessment.open_providers:
            raise ValueError(
                "provider-impact CLEARED requires no open circuit; assessment has "
                f"{list(self.assessment.open_providers)} (issue #{self.issue_number})"
            )

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers this transition is *about*.

        Blocking names only the circuits that are actually open — never the
        issue's other, healthy providers (#5980 F4). Clearing names the
        still-tracked recovering circuits when there are any, because those are
        the ones whose state the operator was waiting on; otherwise it names
        every provider the issue depends on, all of which read healthy.
        """
        if self.transition is ProviderImpactTransition.BLOCKED:
            return self.assessment.open_providers
        if self.assessment.recovering_providers:
            return self.assessment.recovering_providers
        return self.assessment.assessed_providers

    @property
    def next_retry_at(self) -> str | None:
        return self.assessment.next_retry_at

    @property
    def cooldown_remaining_seconds(self) -> int | None:
        return self.assessment.cooldown_remaining_seconds

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

        Blocked entries fold the retry window in ("enter" + "retry" text).
        Cleared entries distinguish "the cooldown elapsed, retry allowed but
        recovery unconfirmed" from "every provider reads healthy" — claiming
        recovery for a merely-recovering circuit would make the audit trail
        wrong (#5980 F4).
        """
        providers = self.provider_list or "provider"
        if self.transition is ProviderImpactTransition.BLOCKED:
            if self.cooldown_remaining_seconds is not None:
                window = format_cooldown(self.cooldown_remaining_seconds)
                return (
                    f"Blocked by provider outage: {providers} unavailable — "
                    f"next retry in {window}"
                )
            return f"Blocked by provider outage: {providers} unavailable"
        if self.assessment.release_kind is ProviderReleaseKind.COOLDOWN_ELAPSED:
            return (
                f"Provider cooldown elapsed: {providers} — issue released to retry "
                "(recovery not confirmed yet)"
            )
        return f"Providers available: {providers} — issue released for retry"

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
            **self.assessment.as_payload(),
        }
        if self.transition is ProviderImpactTransition.CLEARED:
            # Typed counterpart to the summary wording, so consumers can tell a
            # cooldown expiry from a healthy fleet without parsing prose.
            payload["release_kind"] = self.assessment.release_kind.value
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
    "ProviderAvailability",
    "ProviderImpactAssessment",
    "ProviderImpactTransition",
    "ProviderReleaseKind",
    "apply_provider_impact",
    "format_cooldown",
]
