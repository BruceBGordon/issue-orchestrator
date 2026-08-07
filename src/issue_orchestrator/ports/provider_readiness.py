"""Typed provider-readiness / auth-failure boundary (#6999).

One concept — "is this provider authenticated, and is this output an auth
failure" — with exactly one owner. Provider *execution* adapters do all the raw
interpretation (running the CLI's auth probe, reading its TUI banner); control
consumes only the typed :class:`ProviderReadiness` value defined here.

Why this exists: on 2026-08-04 an expired Claude Code login produced four
back-to-back 90-minute zero-work sessions. Every layer that could have caught it
had a partial seam — ``CLIProvider.is_authenticated()`` with no call site, an
auth classification table that only knew HTTP tokens — and the naive fix would
have grown a *third* independent "is this an auth failure" site in the session
watcher. This port is the single typed outcome all three consumers share:

* the launch gate (park instead of spawning a doomed session),
* the live-session observer (fail in minutes with a non-timeout outcome),
* :class:`~issue_orchestrator.control.provider_resilience.ProviderResilienceManager`
  (still the sole circuit-state owner; it consumes typed AUTH outcomes).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .provider_resilience import ProviderErrorType


class ProviderReadinessState(str, Enum):
    """Whether a provider can be launched right now, and why not.

    ``UNKNOWN`` is deliberately distinct from ``READY``: it means "no probe
    could answer", which must not be reported as a positive authentication
    result. It is still launchable — refusing to launch because a provider
    ships no auth probe would be a worse failure than the one being fixed.
    """

    READY = "ready"
    NOT_INSTALLED = "not_installed"
    AUTH_EXPIRED = "auth_expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderReadiness:
    """One provider's launch readiness, as control is allowed to see it.

    Carries no raw CLI text beyond a short human-readable ``detail`` summary:
    the banner/JSON/exit-code interpretation happened in the provider adapter,
    and control never re-derives it.
    """

    provider: str
    state: ProviderReadinessState
    detail: str = ""

    @property
    def launchable(self) -> bool:
        """Whether a session may be spawned for this provider right now."""
        return self.state in (
            ProviderReadinessState.READY,
            ProviderReadinessState.UNKNOWN,
        )

    @property
    def authenticated(self) -> bool:
        """Whether a probe positively confirmed working credentials."""
        return self.state is ProviderReadinessState.READY

    @property
    def human_fixable(self) -> bool:
        """Whether only a human can clear this (agent retries are pure waste)."""
        return self.state is ProviderReadinessState.AUTH_EXPIRED

    @property
    def error_type(self) -> ProviderErrorType | None:
        """The typed circuit-owner input this readiness implies, if any."""
        if self.state is ProviderReadinessState.AUTH_EXPIRED:
            return ProviderErrorType.AUTH
        return None

    @classmethod
    def ready(cls, provider: str, detail: str = "") -> "ProviderReadiness":
        return cls(provider=provider, state=ProviderReadinessState.READY, detail=detail)

    @classmethod
    def auth_expired(cls, provider: str, detail: str) -> "ProviderReadiness":
        return cls(
            provider=provider,
            state=ProviderReadinessState.AUTH_EXPIRED,
            detail=detail,
        )

    @classmethod
    def not_installed(cls, provider: str, detail: str) -> "ProviderReadiness":
        return cls(
            provider=provider,
            state=ProviderReadinessState.NOT_INSTALLED,
            detail=detail,
        )

    @classmethod
    def unknown(cls, provider: str, detail: str = "") -> "ProviderReadiness":
        return cls(
            provider=provider, state=ProviderReadinessState.UNKNOWN, detail=detail
        )


class ProviderReadinessProbe(Protocol):
    """The one surface control asks about provider credentials.

    Both methods return the same typed value so callers never branch on raw
    provider output. ``diagnose_session_output`` exists as its own method
    (rather than a bare "classify this string") because the authoritative
    answer for a live session is *probe confirmation*: an auth-looking banner
    is only a trigger, and the adapter decides whether to confirm it.
    """

    def check_launch_readiness(self, provider: str) -> ProviderReadiness:
        """Answer "may I launch ``provider`` right now?" before spawning."""
        ...

    def diagnose_session_output(
        self, provider: str, output: str
    ) -> ProviderReadiness:
        """Answer "is this live session's output a provider auth failure?"."""
        ...


@dataclass(frozen=True)
class StaticProviderReadinessProbe:
    """A probe that reports a fixed readiness without running anything.

    Deliberately *not* a fallback: it exists so the places that genuinely have
    no provider process to interrogate must name that fact in the type system —
    tests injecting an explicit readiness, and composition paths built before
    any provider adapter is wired. Same pattern as
    :class:`~issue_orchestrator.ports.provider_resilience.StaticProviderCircuitStatusReader`.
    """

    state: ProviderReadinessState = ProviderReadinessState.UNKNOWN
    detail: str = "no provider readiness probe configured"

    def check_launch_readiness(self, provider: str) -> ProviderReadiness:
        return ProviderReadiness(
            provider=provider, state=self.state, detail=self.detail
        )

    def diagnose_session_output(
        self, provider: str, output: str
    ) -> ProviderReadiness:
        del output  # a static probe interprets no output
        return ProviderReadiness(
            provider=provider, state=self.state, detail=self.detail
        )


# The explicit "no provider adapter is wired to probe" reader. Reports UNKNOWN,
# which is launchable — an unprobeable provider must behave exactly as it did
# before this boundary existed.
NO_PROVIDER_READINESS_PROBE: StaticProviderReadinessProbe = (
    StaticProviderReadinessProbe()
)


__all__ = [
    "NO_PROVIDER_READINESS_PROBE",
    "ProviderReadiness",
    "ProviderReadinessProbe",
    "ProviderReadinessState",
    "StaticProviderReadinessProbe",
]
