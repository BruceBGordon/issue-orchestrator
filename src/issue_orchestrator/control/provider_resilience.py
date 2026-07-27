"""Provider resilience manager (circuit breaker control plane)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..events import EventName
from ..ports import EventSink,  make_trace_event
from ..ports.provider_resilience import (
    ProviderCircuitState,
    ProviderCircuitStatus,
    ProviderCircuitStore,
)
from ..infra.config import ProviderResilienceConfig


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderResilienceManager:
    """Circuit breaker manager for AI providers."""

    config: ProviderResilienceConfig
    store: ProviderCircuitStore
    events: EventSink

    def get_state(self, provider: str) -> ProviderCircuitState | None:
        return self.store.get(provider)

    def is_open(self, provider: str, now: datetime | None = None) -> bool:
        if not provider:
            return False
        state = self.store.get(provider)
        if state is None or state.open_until is None:
            return False
        now = now or _now()
        return state.open_until > now

    def snapshot(self, now: datetime | None = None) -> list[ProviderCircuitStatus]:
        """Return the interpreted status of every tracked provider circuit.

        This is the single read surface for UI/observation layers: the "is
        the circuit open right now" and "how much cooldown remains" policy
        lives here (next to :meth:`is_open`), so callers never re-derive it
        from ``open_until``. Results are sorted by provider for stable
        rendering. Providers with no recorded outage are absent (a healthy
        circuit has no row).
        """
        now = now or _now()
        statuses: list[ProviderCircuitStatus] = []
        for state in self.store.list_all():
            open_until = state.open_until
            is_open = open_until is not None and open_until > now
            if is_open and open_until is not None:
                cooldown_remaining = max(0, int((open_until - now).total_seconds()))
            else:
                cooldown_remaining = 0
            statuses.append(ProviderCircuitStatus(
                provider=state.provider,
                is_open=is_open,
                open_until=open_until if is_open else None,
                cooldown_remaining_seconds=cooldown_remaining,
                consecutive_outages=state.consecutive_outages,
                last_error_summary=state.last_error_summary,
                updated_at=state.updated_at,
            ))
        statuses.sort(key=lambda s: s.provider)
        return statuses

    def record_transient_failure(
        self,
        provider: str | None,
        *,
        error_summary: str | None = None,
        attempts: int | None = None,
        now: datetime | None = None,
    ) -> ProviderCircuitState | None:
        if not provider:
            return None

        now = now or _now()
        state = self.store.get(provider)
        consecutive = (state.consecutive_outages + 1) if state else 1

        multiplier = 2 ** max(0, min(consecutive - 1, self.config.circuit_breaker.max_cooldowns - 1))
        cooldown_seconds = self.config.circuit_breaker.cooldown_seconds * multiplier
        open_until = now + timedelta(seconds=cooldown_seconds)

        was_open = state is not None and state.open_until is not None and state.open_until > now

        new_state = ProviderCircuitState(
            provider=provider,
            open_until=open_until,
            consecutive_outages=consecutive,
            last_error_summary=error_summary,
            updated_at=now,
        )
        self.store.save(new_state)

        self.events.publish(make_trace_event(
            EventName.PROVIDER_TRANSIENT_ERROR,
            {
                "provider": provider,
                "attempts": attempts,
                "error_summary": error_summary,
            },
        ))

        if not was_open:
            self.events.publish(make_trace_event(
                EventName.PROVIDER_OUTAGE_ENTERED,
                {
                    "provider": provider,
                    "open_until": open_until.isoformat(),
                    "consecutive_outages": consecutive,
                    "error_summary": error_summary,
                },
            ))

        self.events.publish(make_trace_event(
            EventName.PROVIDER_RETRY_SCHEDULED,
            {
                "provider": provider,
                "open_until": open_until.isoformat(),
                "cooldown_seconds": cooldown_seconds,
                "consecutive_outages": consecutive,
            },
        ))

        return new_state

    def record_success(self, provider: str | None, now: datetime | None = None) -> None:
        if not provider:
            return
        now = now or _now()
        state = self.store.get(provider)
        if state is None:
            return
        self.store.delete(provider)
        self.events.publish(make_trace_event(
            EventName.PROVIDER_OUTAGE_EXITED,
            {
                "provider": provider,
                "at": now.isoformat(),
            },
        ))

    def close_expired(self, now: datetime | None = None) -> list[ProviderCircuitState]:
        now = now or _now()
        closed: list[ProviderCircuitState] = []
        for state in self.store.list_all():
            if state.open_until is None or state.open_until > now:
                continue
            updated = ProviderCircuitState(
                provider=state.provider,
                open_until=None,
                consecutive_outages=state.consecutive_outages,
                last_error_summary=state.last_error_summary,
                updated_at=now,
            )
            self.store.save(updated)
            closed.append(updated)
            self.events.publish(make_trace_event(
                EventName.PROVIDER_OUTAGE_EXITED,
                {
                    "provider": state.provider,
                    "at": now.isoformat(),
                },
            ))
            self.events.publish(make_trace_event(
                EventName.PROVIDER_RETRY_ATTEMPTED,
                {
                    "provider": state.provider,
                    "at": now.isoformat(),
                },
            ))
        return closed
