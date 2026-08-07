"""The one gate every launch path passes before spawning a session (#6999).

Two questions, always in this order:

1. Does the provider's own credential probe say it is authenticated?
2. If so, is its circuit open for some other (transient) reason?

The probe comes first even though it is the more expensive question, because it
is also the only way *out* of an auth outage: while the circuit is open no
session runs, so a gate that short-circuited on the open circuit could never
observe the human re-authenticating and would stall the fleet for the full auth
cooldown. The probe's own short-lived result cache keeps the real cost to about
one local, non-interactive CLI call per provider per minute.

Both answers arrive as typed values. Nothing here reads a banner, an exit code,
or circuit arithmetic: :class:`ProviderAvailabilityPolicy` owns the provider
questions and :class:`~.provider_resilience.ProviderResilienceManager` owns the
circuit. This module owns only the launch consequence — park or proceed — so
the five launch paths (issue, validation retry, review, retrospective review,
rework) cannot drift apart on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink
from ..ports.event_sink import make_trace_event
from .actions import Action
from .provider_availability import ProviderAvailabilityPolicy
from .session_launch_types import LaunchResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderLaunchGate:
    """Decide whether a provider may be launched against, and park it if not."""

    policy: ProviderAvailabilityPolicy
    events: EventSink
    apply_actions: Callable[[list[Action], str], bool]

    def check(self, provider: str | None, issue_number: int) -> Optional[LaunchResult]:
        """Return a parking :class:`LaunchResult`, or ``None`` to proceed."""
        if not provider:
            return None
        outcome = self.policy.assess_launch(provider)
        if not outcome.blocked_by_credentials:
            # Healthy credentials still do not override a transient outage.
            return self._park_for_open_circuit(provider, issue_number)
        readiness = outcome.readiness
        # The assessment may have just tripped the circuit (it feeds typed AUTH
        # outcomes to the circuit owner), so ask for the blocked transition —
        # that is what parks the issue with its durable record.
        parked = self._park_for_open_circuit(provider, issue_number)
        self.events.publish(make_trace_event(
            EventName.SESSION_LAUNCH_FAILED_AUTH,
            {
                "issue_number": issue_number,
                "provider": provider,
                "readiness": readiness.state.value,
                "detail": readiness.detail,
                "human_fixable": readiness.human_fixable,
                "circuit_open": parked is not None,
            },
        ))
        logger.warning(
            issue_log(
                issue_number, "Launch parked: provider=%s readiness=%s detail=%s"
            ),
            provider,
            readiness.state.value,
            readiness.detail,
        )
        return LaunchResult(
            None, False, f"Provider not ready: {provider} ({readiness.state.value})"
        )

    def _park_for_open_circuit(
        self, provider: str, issue_number: int
    ) -> Optional[LaunchResult]:
        # One point-in-time assessment drives both the launch gate and the
        # provider-impact command (blocked label + durable record), so the two
        # can never describe different instants (#5980 F4/A2).
        assessment = self.policy.assess((provider,))
        if not assessment.blocked:
            return None
        self.apply_actions(
            [self.policy.blocked_transition(issue_number, assessment)],
            "provider_unavailable",
        )
        return LaunchResult(None, False, f"Provider unavailable: {provider}")


__all__ = ["ProviderLaunchGate"]
