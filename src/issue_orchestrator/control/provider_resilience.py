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
        return self._is_open_state(self.store.get(provider), now or _now())

    @staticmethod
    def _is_open_state(state: ProviderCircuitState | None, now: datetime) -> bool:
        """Whether *any* cause still holds this circuit open at ``now``."""
        if state is None or state.open_until is None:
            return False
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
        statuses = [self._interpret(state, now) for state in self.store.list_all()]
        statuses.sort(key=lambda s: s.provider)
        return statuses

    def status(self, provider: str, now: datetime | None = None) -> ProviderCircuitStatus | None:
        """Interpreted status of one provider's circuit, or ``None`` if untracked.

        Same policy as :meth:`snapshot`, addressed by provider — used by the
        provider-availability owner to stamp "when does this provider retry
        next" onto the issue-scoped provider-impact record.
        """
        if not provider:
            return None
        state = self.store.get(provider)
        if state is None:
            return None
        return self._interpret(state, now or _now())

    @staticmethod
    def _interpret(state: ProviderCircuitState, now: datetime) -> ProviderCircuitStatus:
        open_until = state.open_until
        is_open = open_until is not None and open_until > now
        cooldown_remaining = (
            max(0, int((open_until - now).total_seconds()))
            if is_open and open_until is not None
            else 0
        )
        return ProviderCircuitStatus(
            provider=state.provider,
            is_open=is_open,
            open_until=open_until if is_open else None,
            cooldown_remaining_seconds=cooldown_remaining,
            consecutive_outages=state.consecutive_outages,
            last_error_summary=state.last_error_summary,
            updated_at=state.updated_at,
        )

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

        was_open = self._is_open_state(state, now)

        new_state = ProviderCircuitState(
            provider=provider,
            transient_open_until=open_until,
            # The auth dimension is untouched: a service outage neither proves
            # nor disproves anything about the credentials (#6999 F3).
            auth_open_until=state.auth_open_until if state else None,
            consecutive_outages=consecutive,
            last_error_summary=error_summary,
            updated_at=now,
            consecutive_auth_failures=state.consecutive_auth_failures if state else 0,
            last_auth_sample_id=state.last_auth_sample_id if state else "",
            # Untouched for the same reason as the auth dimension: a service
            # outage says nothing about the account balance.
            quota_open_until=state.quota_open_until if state else None,
            consecutive_quota_failures=(
                state.consecutive_quota_failures if state else 0
            ),
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

    def record_auth_failure(
        self,
        provider: str | None,
        *,
        error_summary: str,
        sample_id: str,
        now: datetime | None = None,
    ) -> ProviderCircuitState | None:
        """Record one typed AUTH *observation* for ``provider``.

        The only way an auth failure reaches circuit state. Callers hand over a
        typed outcome (from the provider-readiness boundary) and this owner
        decides everything else: how many consecutive failures are tolerated,
        how long the circuit stays open, and which events describe it. No
        launcher or watcher computes circuit state (#6999).

        ``sample_id`` identifies the *physical* probe result the outcome came
        from. One credential probe gates every launch in a tick and every caller
        is served the same cached answer, so counting per call would turn one
        observation into N failures and blow straight through any threshold > 1
        (#6999 F2). Re-recording a sample already counted here is a no-op: same
        state, no counter movement, no event. An empty ``sample_id`` means "not
        from a probe" (e.g. a live session's own death) and is always counted.

        An expired credential is human-fixable, so the auth cooldown is its own
        (long) window rather than the transient escalation ladder: retrying on
        the transient schedule is exactly how one expired login burned four
        90-minute sessions. Recovery is not gated on that window elapsing —
        :meth:`clear_auth_failures` retires it the moment the probe confirms
        re-authentication.

        Returns the stored state, or ``None`` when there is no provider to
        record against.
        """
        if not provider:
            return None

        now = now or _now()
        state = self.store.get(provider)
        if state is not None and sample_id and state.last_auth_sample_id == sample_id:
            return state

        consecutive_auth = (state.consecutive_auth_failures + 1) if state else 1
        threshold = self.config.circuit_breaker.auth_failure_threshold
        trips = consecutive_auth >= threshold

        was_open = self._is_open_state(state, now)
        auth_open_until = (
            now + timedelta(seconds=self.config.circuit_breaker.auth_cooldown_seconds)
            if trips
            else (state.auth_open_until if state else None)
        )

        new_state = ProviderCircuitState(
            provider=provider,
            transient_open_until=state.transient_open_until if state else None,
            auth_open_until=auth_open_until,
            consecutive_outages=state.consecutive_outages if state else 0,
            last_error_summary=error_summary,
            updated_at=now,
            consecutive_auth_failures=consecutive_auth,
            last_auth_sample_id=sample_id,
            # A renewed login does not buy credits, and an expired one does not
            # spend them: the quota dimension is orthogonal and survives here.
            quota_open_until=state.quota_open_until if state else None,
            consecutive_quota_failures=(
                state.consecutive_quota_failures if state else 0
            ),
        )
        self.store.save(new_state)

        self.events.publish(make_trace_event(
            EventName.PROVIDER_AUTH_FAILED,
            {
                "provider": provider,
                "consecutive_auth_failures": consecutive_auth,
                "threshold": threshold,
                "circuit_open": trips,
                "error_summary": error_summary,
            },
        ))

        if trips and not was_open and auth_open_until is not None:
            self.events.publish(make_trace_event(
                EventName.PROVIDER_OUTAGE_ENTERED,
                {
                    "provider": provider,
                    "open_until": auth_open_until.isoformat(),
                    "consecutive_outages": new_state.consecutive_outages,
                    "error_summary": error_summary,
                },
            ))

        return new_state

    def record_quota_failure(
        self,
        provider: str | None,
        *,
        error_summary: str,
        now: datetime | None = None,
    ) -> ProviderCircuitState | None:
        """Record an exhausted balance or usage allowance for ``provider``.

        Trips on the **first** observation, unlike the auth ladder. An auth
        verdict comes from one cached probe sample that gates every launch in a
        tick, which is why a threshold above one is meaningful there. A quota
        verdict has no probe behind it: it is read from a session that really
        ran and really failed, so waiting for a second observation means
        deliberately burning a second session to learn what the first already
        proved (#7096).

        Shares :attr:`auth_cooldown_seconds`. Both causes are outages only a
        person can end, and that window exists to keep the fleet off the
        transient ladder; the property that matters is "long", and a second
        knob would carry no information the first does not.

        Recovery is proved either by a later successful provider call through
        :meth:`record_success` or by the elapsed deadline through
        :meth:`close_expired`. A successful call is direct evidence that the
        account can do work again; unlike a credential-only READY probe, it is
        therefore allowed to retire this dimension early.
        """
        if not provider:
            return None

        now = now or _now()
        state = self.store.get(provider)
        consecutive_quota = (state.consecutive_quota_failures + 1) if state else 1
        quota_open_until = now + timedelta(
            seconds=self.config.circuit_breaker.auth_cooldown_seconds
        )
        was_open = self._is_open_state(state, now)

        new_state = ProviderCircuitState(
            provider=provider,
            transient_open_until=state.transient_open_until if state else None,
            auth_open_until=state.auth_open_until if state else None,
            consecutive_outages=state.consecutive_outages if state else 0,
            last_error_summary=error_summary,
            updated_at=now,
            consecutive_auth_failures=state.consecutive_auth_failures if state else 0,
            last_auth_sample_id=state.last_auth_sample_id if state else "",
            quota_open_until=quota_open_until,
            consecutive_quota_failures=consecutive_quota,
        )
        self.store.save(new_state)

        self.events.publish(make_trace_event(
            EventName.PROVIDER_QUOTA_EXHAUSTED,
            {
                "provider": provider,
                "consecutive_quota_failures": consecutive_quota,
                "error_summary": error_summary,
            },
        ))

        if not was_open:
            self.events.publish(make_trace_event(
                EventName.PROVIDER_OUTAGE_ENTERED,
                {
                    "provider": provider,
                    "open_until": quota_open_until.isoformat(),
                    "consecutive_outages": new_state.consecutive_outages,
                    "error_summary": error_summary,
                },
            ))

        return new_state

    def clear_auth_failures(
        self, provider: str | None, now: datetime | None = None
    ) -> ProviderCircuitState | None:
        """Retire the *auth* outage after the credential probe confirms recovery.

        Recovery must NOT wait out :attr:`auth_cooldown_seconds`. That window is
        long precisely because only a human can end a credential outage — and
        the moment they do, the probe says so. Leaving the fleet parked for the
        remaining hours would trade one stall for a longer one.

        Only the auth dimension is retired. A ``READY`` credential probe is
        evidence about credentials and nothing else, so a concurrently valid
        transient outage keeps its own deadline and the provider stays open
        until that deadline passes (#6999 F3). ``provider.outage_exited`` is
        therefore emitted on the *aggregate* transition, never on the auth half
        alone — the fleet must not be told a provider recovered while it is
        still refusing calls.

        Returns the updated state, or ``None`` when there was no auth outage.
        """
        if not provider:
            return None
        state = self.store.get(provider)
        if state is None or (
            state.consecutive_auth_failures == 0 and state.auth_open_until is None
        ):
            return None

        now = now or _now()
        was_open = self._is_open_state(state, now)
        updated = ProviderCircuitState(
            provider=provider,
            transient_open_until=state.transient_open_until,
            auth_open_until=None,
            consecutive_outages=state.consecutive_outages,
            last_error_summary=state.last_error_summary,
            updated_at=now,
            consecutive_auth_failures=0,
            last_auth_sample_id="",
            # A READY credential probe is evidence about credentials alone. It
            # cannot see the balance — no provider CLI exposes one — so a quota
            # outage keeps its deadline and the provider stays closed on it.
            quota_open_until=state.quota_open_until,
            consecutive_quota_failures=state.consecutive_quota_failures,
        )
        self.store.save(updated)

        if was_open and not self._is_open_state(updated, now):
            self.events.publish(make_trace_event(
                EventName.PROVIDER_OUTAGE_EXITED,
                {
                    "provider": provider,
                    "at": now.isoformat(),
                },
            ))
        return updated

    def record_success(
        self, provider: str | None, now: datetime | None = None
    ) -> ProviderCircuitState | None:
        """Retire the *transient* and *quota* outages after a call came back clean.

        Cause-specific, for the same reason :meth:`clear_auth_failures` is
        (#6999 F3). A completed provider call proves both that the service
        answered and that the account had enough allowance to perform work, so
        it retires transient and quota state. This also bounds the impact of a
        false-positive quota classification to the next successful call.

        Success does not prove that a concurrently recorded credential outage
        has recovered, and it is not the typed READY readiness probe the auth
        recovery contract requires. Deleting the whole row here erased the auth
        deadline, auth counter and sample identity that had been recorded by a
        *different* concurrent session, so:

        * an in-flight success landing after an auth outage opened re-admitted
          the whole fleet to a provider that would refuse every launch — the
          90-minute burn this boundary exists to end;
        * ``auth_failure_threshold > 1`` could never accumulate, because any
          successful call in between reset the count to zero;
        * ``provider.outage_exited`` was announced while the provider was still
          demonstrably closed.

        The auth dimension therefore survives untouched and only
        :meth:`clear_auth_failures` — driven by a confirmed READY probe — may
        retire it. The row is deleted only when NO cause is left, which keeps
        "a healthy circuit has no row" true. ``provider.outage_exited`` is
        emitted on the aggregate transition alone.

        Returns the updated state, or ``None`` when the row was deleted or
        there was nothing tracked to update.
        """
        if not provider:
            return None
        now = now or _now()
        state = self.store.get(provider)
        if state is None:
            return None

        was_open = self._is_open_state(state, now)
        updated: ProviderCircuitState | None = ProviderCircuitState(
            provider=provider,
            transient_open_until=None,
            # Untouched: a service call that succeeded says nothing about the
            # credential, and the auth cause is retired by a READY probe only.
            auth_open_until=state.auth_open_until,
            consecutive_outages=0,
            last_error_summary=state.last_error_summary,
            updated_at=now,
            consecutive_auth_failures=state.consecutive_auth_failures,
            last_auth_sample_id=state.last_auth_sample_id,
            # A call that completed is positive evidence that the account has
            # usable allowance again, so quota can retire before its deadline.
            quota_open_until=None,
            consecutive_quota_failures=0,
        )
        auth_cause_remains = (
            state.auth_open_until is not None or state.consecutive_auth_failures > 0
        )
        if auth_cause_remains:
            self.store.save(updated)
        else:
            # Nothing is left to remember, and a healthy circuit has no row.
            self.store.delete(provider)
            updated = None

        if was_open and not self._is_open_state(updated, now):
            self.events.publish(make_trace_event(
                EventName.PROVIDER_OUTAGE_EXITED,
                {
                    "provider": provider,
                    "at": now.isoformat(),
                },
            ))
        return updated

    def close_expired(self, now: datetime | None = None) -> list[ProviderCircuitState]:
        """Retire circuits whose causes have *all* elapsed.

        The aggregate deadline is the latest of the per-cause ones, so a
        provider whose transient cooldown ran out while a much longer auth
        window is still running is skipped here entirely: no half-retirement,
        and no ``outage_exited`` claimed while the provider still refuses calls.
        """
        now = now or _now()
        closed: list[ProviderCircuitState] = []
        for state in self.store.list_all():
            if state.open_until is None or state.open_until > now:
                continue
            updated = ProviderCircuitState(
                provider=state.provider,
                transient_open_until=None,
                auth_open_until=None,
                consecutive_outages=state.consecutive_outages,
                last_error_summary=state.last_error_summary,
                updated_at=now,
                consecutive_auth_failures=state.consecutive_auth_failures,
                last_auth_sample_id=state.last_auth_sample_id,
                # Quota has no probe-driven recovery, so this elapsed deadline
                # is the guaranteed way back when no later success arrives.
                # Retiring the counter with the deadline means the next
                # exhaustion starts fresh rather than using a stale count.
                quota_open_until=None,
                consecutive_quota_failures=0,
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
