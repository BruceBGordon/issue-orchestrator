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
    FATAL = "fatal"


@dataclass(frozen=True)
class ProviderCircuitState:
    provider: str
    open_until: datetime | None
    consecutive_outages: int
    last_error_summary: str | None
    updated_at: datetime
    # Auth failures are counted separately from transient outages: a credential
    # outage is human-fixable and must trip the circuit on its own threshold,
    # not be diluted by unrelated network blips (#6999).
    consecutive_auth_failures: int = 0


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
