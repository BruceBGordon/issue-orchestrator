"""Provider circuit breaker ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class ProviderErrorType(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    QUOTA = "quota"
    FATAL = "fatal"

    @property
    def requires_human_intervention(self) -> bool:
        """Whether waiting can never clear this failure.

        The distinction the retry ladder and the circuit both turn on. A
        transient outage and a rate limit heal on a timer; an expired
        credential and an exhausted balance heal only when a person acts.
        Call sites that mean "a human must fix this" branch on this predicate
        rather than on ``is AUTH``, so adding a second human-fixable cause
        does not silently reinstate the retry-until-the-wall-clock behaviour
        that AUTH was given its own window to prevent (#6999).
        """
        return self in (ProviderErrorType.AUTH, ProviderErrorType.QUOTA)


@dataclass(frozen=True)
class ProviderCircuitState:
    """Persisted circuit state, with one deadline per independent cause.

    A credential outage and a service outage are unrelated facts about a
    provider, and each has its own validity window. Collapsing them into a
    single ``open_until`` meant retiring one silently released the other: a
    ``READY`` credential probe says nothing about a 429/5xx outage, but would
    have re-opened launches into it (#6999 F3). An exhausted balance is a third
    such fact, independent of both: credits are not restored by a service
    recovering, nor by a login being renewed. Every dimension is stored, and
    "is the circuit open" is *derived* from them.
    """

    provider: str
    consecutive_outages: int
    last_error_summary: str | None
    updated_at: datetime
    # Deadline for the escalating transient ladder (429/5xx/transport).
    transient_open_until: datetime | None = None
    # Observation watermark for transient state. Success may arrive out of
    # attempt order and can retire only an outage older than itself.
    transient_observed_at: datetime | None = None
    # Deadline for the human-fixable credential outage. Its own (long) window,
    # cleared the moment the probe confirms re-authentication.
    auth_open_until: datetime | None = None
    # Auth failures are counted separately from transient outages: a credential
    # outage is human-fixable and must trip the circuit on its own threshold,
    # not be diluted by unrelated network blips (#6999).
    consecutive_auth_failures: int = 0
    # Identity of the last provider-readiness *sample* counted against this
    # circuit. One physical credential probe gates many launches in a tick, and
    # every one of them reports the same result; the circuit counts that sample
    # exactly once so a configured threshold > 1 still means "more than one
    # observation" (#6999 F2).
    last_auth_sample_id: str = ""
    # Deadline for an exhausted balance or usage allowance. A third dimension
    # rather than a reuse of the auth window, because the two recover on
    # different signals: a readiness probe confirming a valid login proves
    # nothing about restored credits, while a successful provider call does.
    # Clearing the auth window must therefore not release a quota outage.
    quota_open_until: datetime | None = None
    consecutive_quota_failures: int = 0
    # Observation watermark for the quota dimension. Completion effects can be
    # applied out of attempt order, so recovery evidence must be newer than the
    # exhaustion fact it retires.
    quota_observed_at: datetime | None = None

    @property
    def open_until(self) -> datetime | None:
        """The latest deadline across all causes, or ``None`` if none are set.

        The aggregate the circuit is judged on: a provider is unavailable while
        *any* cause is still within its own window.
        """
        deadlines = [
            deadline
            for deadline in (
                self.transient_open_until,
                self.auth_open_until,
                self.quota_open_until,
            )
            if deadline is not None
        ]
        return max(deadlines) if deadlines else None


@dataclass(frozen=True)
class ProviderCircuitStatus:
    """Derived, point-in-time read model of a provider's circuit.

    Unlike the persisted :class:`ProviderCircuitState`, this carries the
    *interpreted* status the circuit owner computes against a clock:
    whether the circuit is open right now and how much cooldown remains.
    UI/observation layers consume this instead of re-deriving "is open"
    from ``open_until`` (that policy lives once, on the manager).
    """

    provider: str
    is_open: bool
    open_until: datetime | None
    cooldown_remaining_seconds: int
    consecutive_outages: int
    last_error_summary: str | None
    updated_at: datetime


class ProviderCircuitStatusReader(Protocol):
    """Narrow read port: the interpreted status of every tracked circuit.

    The only surface presentation code is allowed to depend on for provider
    circuit state. Implemented by the circuit owner
    (``control.provider_resilience.ProviderResilienceManager``), so the
    dashboard projection depends on *behaviour* ("give me the interpreted
    status") rather than on the orchestrator's dependency-container layout.
    """

    def snapshot(self, now: datetime | None = None) -> list[ProviderCircuitStatus]:
        ...


@dataclass(frozen=True)
class StaticProviderCircuitStatusReader:
    """A reader that returns a fixed, explicitly supplied status list.

    Deliberately *not* a fallback: it exists so the two places that genuinely
    have no circuit owner to read must name that fact in the type system —
    the pre-boot dashboard page (no orchestrator is installed yet) and tests
    that inject an explicit circuit state. A misconfigured production
    orchestrator can never silently resolve to this, because the orchestrator
    facade exposes its resilience owner as a required property.
    """

    statuses: tuple[ProviderCircuitStatus, ...] = ()

    def snapshot(self, now: datetime | None = None) -> list[ProviderCircuitStatus]:
        del now  # a fixed status list has no clock to interpret against
        return list(self.statuses)


# The explicit "there is no circuit owner to read" reader. Distinct from a
# healthy-but-empty read only in intent; both render as "no outage", which is
# correct when no orchestrator is running at all.
NO_PROVIDER_CIRCUIT_STATUS: StaticProviderCircuitStatusReader = (
    StaticProviderCircuitStatusReader()
)


class ProviderCircuitStore(Protocol):
    """Persistence for provider circuit breaker state."""

    def get(self, provider: str) -> ProviderCircuitState | None:
        ...

    def list_all(self) -> list[ProviderCircuitState]:
        ...

    def save(self, state: ProviderCircuitState) -> None:
        ...

    def delete(self, provider: str) -> None:
        ...


class InMemoryProviderCircuitStore:
    """In-memory store for tests."""

    def __init__(self) -> None:
        self._states: dict[str, ProviderCircuitState] = {}

    def get(self, provider: str) -> ProviderCircuitState | None:
        return self._states.get(provider)

    def list_all(self) -> list[ProviderCircuitState]:
        return list(self._states.values())

    def save(self, state: ProviderCircuitState) -> None:
        self._states[state.provider] = state

    def delete(self, provider: str) -> None:
        self._states.pop(provider, None)
