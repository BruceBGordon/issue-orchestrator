"""CLI-backed provider readiness probe (#6999).

The adapter side of :mod:`issue_orchestrator.ports.provider_readiness`: it
resolves a configured provider name to its CLI adapter, runs that adapter's
cheap non-interactive credential probe, and hands control one typed
:class:`ProviderReadiness`. No banner text, exit code, or JSON crosses back.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from ..ports.command_runner import CommandRunner
from ..ports.provider_readiness import ProviderReadiness, ProviderReadinessState
from ..ports.provider_resilience import ProviderErrorType
from .agent_runner_providers import CLIProvider, get_provider

logger = logging.getLogger(__name__)

# A credential probe answers from local state, so re-running it for every
# launch in a tick is pure overhead. It is short enough that a human who
# re-authenticates is unblocked on the next tick, not the next hour.
DEFAULT_PROBE_TTL_SECONDS = 60.0


class CLIProviderReadinessProbe:
    """Probe configured provider CLIs for launch readiness.

    Results are cached for ``ttl_seconds`` per provider: a single tick may gate
    several launches on the same provider, and each would otherwise spawn its
    own subprocess.
    """

    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        ttl_seconds: float = DEFAULT_PROBE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        resolve_provider: Callable[[str], CLIProvider] = get_provider,
    ) -> None:
        self._runner = command_runner
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._resolve_provider = resolve_provider
        self._cache: dict[str, tuple[float, ProviderReadiness]] = {}

    def check_launch_readiness(self, provider: str) -> ProviderReadiness:
        """Typed answer to "may I launch ``provider`` right now?".

        The returned value is stamped with the id of the probe *execution* that
        produced it, and every caller served from the cache below gets that
        same id back. The circuit owner keys on it, so a tick that gates ten
        launches on one cached ``AUTH_EXPIRED`` result records one auth failure
        rather than ten (#6999 F2).

        The id is a UUID rather than a counter because the circuit owner
        *persists* the last one it counted. A per-process counter would restart
        at the same value every boot and collide with the stored id, so the
        first real observation after a restart would be discarded as a replay —
        and with a threshold above 1, repeated restarts could keep the circuit
        from ever tripping.
        """
        if not provider:
            return ProviderReadiness.unknown("", "no provider configured")
        cached = self._cached(provider)
        if cached is not None:
            return cached
        readiness = replace(self._probe(provider), sample_id=uuid.uuid4().hex)
        self._cache[provider] = (self._clock(), readiness)
        return readiness

    def diagnose_session_output(
        self, provider: str, output: str
    ) -> ProviderReadiness:
        """Typed answer to "is this live session's output a provider auth failure?".

        The output signature is only a *trigger*: an orchestrator working on its
        own auth tooling routinely prints the very banner it is matching on. The
        authoritative answer is the provider's own credential probe, so a
        triggered diagnosis is confirmed before it can fail a session. That
        keeps one classification table and still makes false positives
        impossible to act on.
        """
        if not provider or not output:
            return ProviderReadiness.unknown(provider, "nothing to diagnose")
        try:
            adapter = self._resolve_provider(provider)
        except ValueError:
            return ProviderReadiness.unknown(provider, f"unknown provider {provider!r}")
        if adapter.classify_output(output) is not ProviderErrorType.AUTH:
            return ProviderReadiness.unknown(
                provider, "session output shows no auth-failure signature"
            )
        confirmation = self.check_launch_readiness(provider)
        if confirmation.state is ProviderReadinessState.AUTH_EXPIRED:
            return confirmation
        return ProviderReadiness.unknown(
            provider,
            "auth-failure signature not confirmed by the provider credential probe",
        )

    def _cached(self, provider: str) -> ProviderReadiness | None:
        entry = self._cache.get(provider)
        if entry is None:
            return None
        probed_at, readiness = entry
        if self._clock() - probed_at >= self._ttl_seconds:
            return None
        return readiness

    def _probe(self, provider: str) -> ProviderReadiness:
        try:
            adapter = self._resolve_provider(provider)
        except ValueError:
            return ProviderReadiness.unknown(provider, f"unknown provider {provider!r}")
        try:
            readiness = adapter.check_readiness(self._runner)
        except OSError as exc:
            # A probe that cannot run tells us nothing about credentials;
            # reporting UNKNOWN keeps the launch path unchanged rather than
            # parking the fleet on an infrastructure hiccup.
            logger.warning(
                "[provider] readiness probe for %s could not run: %s", provider, exc
            )
            return ProviderReadiness.unknown(provider, f"probe could not run: {exc}")
        if readiness.state is ProviderReadinessState.AUTH_EXPIRED:
            logger.warning("[provider] %s is not authenticated: %s", provider, readiness.detail)
        return readiness


__all__ = ["CLIProviderReadinessProbe", "DEFAULT_PROBE_TTL_SECONDS"]
