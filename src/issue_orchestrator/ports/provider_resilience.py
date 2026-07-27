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
