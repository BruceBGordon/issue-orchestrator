"""Projection of provider circuit-breaker status into a UI payload.

Presentation only: takes the interpreted :class:`ProviderCircuitStatus`
snapshot the circuit owner (:class:`ProviderResilienceManager`) produces and
turns it into the strings/flags the dashboard banner + health panel render.
No circuit *policy* lives here — "is the circuit open" and "how much cooldown
remains" are decided by the manager; this module only formats them.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict

from ..ports.provider_resilience import ProviderCircuitStatus

logger = logging.getLogger(__name__)


class ProviderCircuitBase(BaseModel):
    """Frozen, strict base for the circuit UI payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderCircuitEntryView(ProviderCircuitBase):
    """One provider's circuit row for the health panel."""

    provider: str
    is_open: bool
    # Text status (never colour-only): "Unavailable" while the circuit is
    # open, "Recovering" once the cooldown has elapsed but the provider is
    # still tracked (a retry has not yet succeeded).
    status_label: str
    # Human cooldown label ("4m 12s") while open; ``None`` once recovering.
    cooldown_remaining_label: str | None
    # ISO-8601 instant the circuit next allows a retry (``open_until``) while
    # open; ``None`` once recovering.
    next_retry_at: str | None
    consecutive_outages: int
    last_error_summary: str | None


class ProviderCircuitStatusView(ProviderCircuitBase):
    """Top-level circuit status powering the banner + health panel."""

    # True when at least one provider circuit is open right now. The banner
    # shows iff this is set (or ``status_unavailable`` is).
    any_open: bool
    open_count: int
    open_providers: tuple[str, ...]
    # One-line, colour-independent summary for the banner.
    summary_text: str
    # Soonest ``open_until`` across all open circuits (ISO-8601), or ``None``.
    next_retry_at: str | None
    entries: tuple[ProviderCircuitEntryView, ...]
    # True when the circuit state could NOT be read/projected (corrupt store,
    # broken manager, unexpected projection bug). This is distinct from
    # ``any_open=False`` (a genuinely healthy fleet): a read failure must never
    # masquerade as "no outage", so the banner renders it as a health warning
    # instead of hiding — otherwise a broken read would silently conceal a real
    # provider outage from operators (issue #5980).
    status_unavailable: bool = False

    @classmethod
    def empty(cls) -> "ProviderCircuitStatusView":
        return cls(
            any_open=False,
            open_count=0,
            open_providers=(),
            summary_text="",
            next_retry_at=None,
            entries=(),
            status_unavailable=False,
        )

    @classmethod
    def unavailable(cls, detail: str | None = None) -> "ProviderCircuitStatusView":
        """Explicit degraded status for a failed circuit read/projection.

        Rendered as a warning banner (not hidden) so operators see that the
        provider-circuit status is unknown rather than assuming all is well.
        """
        summary = "Provider circuit status unavailable — could not read circuit state."
        if detail:
            summary = f"{summary} ({detail})"
        return cls(
            any_open=False,
            open_count=0,
            open_providers=(),
            summary_text=summary,
            next_retry_at=None,
            entries=(),
            status_unavailable=True,
        )


def _format_duration(seconds: int) -> str:
    """Compact ``"1h 4m"`` / ``"4m 12s"`` / ``"12s"`` cooldown label."""
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


def _entry(status: ProviderCircuitStatus) -> ProviderCircuitEntryView:
    return ProviderCircuitEntryView(
        provider=status.provider,
        is_open=status.is_open,
        status_label="Unavailable" if status.is_open else "Recovering",
        cooldown_remaining_label=(
            _format_duration(status.cooldown_remaining_seconds) if status.is_open else None
        ),
        next_retry_at=status.open_until.isoformat() if status.is_open and status.open_until else None,
        consecutive_outages=status.consecutive_outages,
        last_error_summary=status.last_error_summary,
    )


def _summary_text(open_entries: Sequence[ProviderCircuitEntryView]) -> str:
    if not open_entries:
        return ""
    names = ", ".join(e.provider for e in open_entries)
    soonest = _soonest_retry_label(open_entries)
    retry = f" — next retry in {soonest}" if soonest else ""
    if len(open_entries) == 1:
        return f"Provider outage: {names} unavailable{retry}."
    return f"Provider outage: {len(open_entries)} providers unavailable ({names}){retry}."


def _soonest_retry_label(open_entries: Sequence[ProviderCircuitEntryView]) -> str | None:
    labels = [e.cooldown_remaining_label for e in open_entries if e.cooldown_remaining_label]
    if not labels:
        return None
    # open_entries are sorted by provider, not cooldown; pick the smallest
    # remaining window so the banner advertises the *next* retry.
    return min(labels, key=_label_seconds)


def _label_seconds(label: str) -> int:
    """Best-effort ordering key for compact duration labels (``"4m 12s"``)."""
    total = 0
    for part in label.split():
        unit = part[-1]
        try:
            value = int(part[:-1])
        except ValueError:
            continue
        total += value * {"h": 3600, "m": 60, "s": 1}.get(unit, 0)
    return total


def build_provider_circuit_status(
    statuses: Sequence[ProviderCircuitStatus],
) -> ProviderCircuitStatusView:
    """Project the manager's circuit snapshot into the dashboard payload.

    ``statuses`` is the output of :meth:`ProviderResilienceManager.snapshot`.
    Open circuits are listed first (then recovering), each already sorted by
    provider name upstream, so the panel order is stable.
    """
    entries = tuple(_entry(s) for s in statuses)
    ordered = tuple(sorted(entries, key=lambda e: (not e.is_open, e.provider)))
    open_entries = tuple(e for e in ordered if e.is_open)
    next_retry = None
    open_next_retry = [e.next_retry_at for e in open_entries if e.next_retry_at]
    if open_next_retry:
        next_retry = min(open_next_retry)
    return ProviderCircuitStatusView(
        any_open=bool(open_entries),
        open_count=len(open_entries),
        open_providers=tuple(e.provider for e in open_entries),
        summary_text=_summary_text(open_entries),
        next_retry_at=next_retry,
        entries=ordered,
    )


def read_provider_circuit_status(orchestrator: Any) -> ProviderCircuitStatusView:
    """Read the circuit owner and project its snapshot for the dashboard.

    Missing pre-bootstrap dependencies are the one healthy-empty case. Once a
    provider-resilience manager exists, a read or projection failure becomes an
    explicit unavailable payload so a broken store cannot masquerade as a
    healthy provider fleet.
    """
    if orchestrator is None:
        return ProviderCircuitStatusView.empty()
    deps = getattr(orchestrator, "deps", None)
    if deps is None:
        return ProviderCircuitStatusView.empty()
    manager = getattr(deps, "provider_resilience", None)
    if manager is None:
        return ProviderCircuitStatusView.empty()
    try:
        return build_provider_circuit_status(manager.snapshot())
    except Exception as exc:  # noqa: BLE001 - failure is rendered as an explicit warning
        logger.exception("provider circuit status read/projection failed")
        return ProviderCircuitStatusView.unavailable(str(exc))
