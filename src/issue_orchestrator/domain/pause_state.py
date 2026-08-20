"""The vocabulary of an orchestrator PAUSE: why, who, and since when.

A paused engine looks identical from the outside no matter what stopped it —
an operator clicking Pause, a tech-lead run halting the planner for the length
of an investigation, or the loop-error breaker tripping on three consecutive
tick failures. Those are very different situations: two are deliberate and
self-clearing, the third is an incident that will never clear on its own.

Before these types the distinction was unrecorded. ``OrchestratorState.paused``
was a bare ``bool`` assigned from four modules; five of the seven pause paths
published an empty event payload, two published nothing at all, and none of it
was ever persisted (the timeline writer drops any event without an integer
``issue_number``, and a pause has none). Answering "why is the engine paused?"
meant grepping a multi-hundred-megabyte log for the single ``Pausing
orchestrator`` line — which, for a pause that had held for days, sat hundreds of
thousands of lines above the current tail, and vanished entirely on restart.

So the vocabulary lives here, in the domain, rather than beside the controller
that applies it (:mod:`..control.pause_controller`). That separation is what
lets a view model, an HTTP route, or a test *describe* a pause without importing
the transition machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class PauseReason(StrEnum):
    """Why the engine stopped planning new work.

    ``OPERATOR`` is the only reason a human chose directly. ``STARTUP`` and the
    two ``TECH_LEAD_*`` reasons are deliberate and expected to clear on their
    own. ``LOOP_ERROR_THRESHOLD`` is the incident case: the breaker tripped and
    nothing will resume the engine without intervention.
    """

    OPERATOR = "operator"
    STARTUP = "startup"
    LOOP_ERROR_THRESHOLD = "loop_error_threshold"
    TECH_LEAD_INVESTIGATION = "tech_lead_investigation"
    TECH_LEAD_HEALTH_REVIEW = "tech_lead_health_review"

    @property
    def is_incident(self) -> bool:
        """True when this pause represents a fault rather than an intent.

        The dashboard and status payloads use this to separate "someone paused
        this" from "this engine fell over and is waiting for a human".
        """
        return self is PauseReason.LOOP_ERROR_THRESHOLD


class PauseActor(StrEnum):
    """Which surface requested the transition.

    ``SYSTEM`` covers transitions no caller asked for — the loop-error breaker
    tripping — and is deliberately distinct from every operator-facing surface
    so an incident is never mistaken for a human decision.
    """

    WEB_API = "web_api"
    CONTROL_API = "control_api"
    MCP = "mcp"
    DASHBOARD = "dashboard"
    CLI = "cli"
    SYSTEM = "system"


@dataclass(frozen=True)
class PauseState:
    """The engine's pause status and, when paused, its full provenance.

    ``reason``/``actor``/``since`` are non-``None`` exactly when ``paused`` is
    ``True``; the invariant is enforced in ``__post_init__`` rather than left to
    callers, so a paused state that cannot explain itself is unconstructible.
    """

    paused: bool = False
    reason: PauseReason | None = None
    actor: PauseActor | None = None
    detail: str = ""
    since: datetime | None = None

    def __post_init__(self) -> None:
        if self.paused and (self.reason is None or self.actor is None or self.since is None):
            raise ValueError(
                "A paused PauseState requires reason, actor, and since — "
                "an unexplained pause is the bug this type exists to prevent."
            )
        if not self.paused and (self.reason is not None or self.actor is not None):
            raise ValueError(
                "A running PauseState carries no reason or actor; "
                f"got reason={self.reason!r} actor={self.actor!r}."
            )

    @classmethod
    def running(cls) -> "PauseState":
        """The not-paused state."""
        return cls()

    @classmethod
    def paused_now(
        cls,
        *,
        reason: PauseReason,
        actor: PauseActor,
        detail: str = "",
        now: datetime | None = None,
    ) -> "PauseState":
        """A paused state stamped at ``now``, defaulting to the current time."""
        return cls(
            paused=True,
            reason=reason,
            actor=actor,
            detail=detail,
            since=_as_utc(now if now is not None else datetime.now(timezone.utc)),
        )

    def held_seconds(self, now: datetime) -> float:
        """How long this pause has held, in seconds. ``0.0`` when running."""
        if not self.paused or self.since is None:
            return 0.0
        return max(0.0, (_as_utc(now) - self.since).total_seconds())

    def describe(self, now: datetime | None = None) -> str:
        """One-line human summary for logs and operator-facing surfaces.

        This is the string that turns a steady-state ``paused=True`` log line
        from a dead end into an answer.
        """
        if not self.paused:
            return "running"
        parts = [f"reason={self.reason}", f"by={self.actor}"]
        if self.since is not None:
            parts.append(f"since={self.since.isoformat()}")
        if now is not None:
            parts.append(f"held={self.held_seconds(now):.0f}s")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)

    def to_payload(self, now: datetime | None = None) -> dict[str, Any]:
        """Serialize for event payloads, status routes, and view models."""
        return {
            "paused": self.paused,
            "pause_reason": str(self.reason) if self.reason is not None else None,
            "pause_actor": str(self.actor) if self.actor is not None else None,
            "pause_detail": self.detail,
            "paused_since": self.since.isoformat() if self.since is not None else None,
            "paused_held_seconds": self.held_seconds(now) if now is not None else None,
            "pause_is_incident": bool(self.reason is not None and self.reason.is_incident),
        }


@dataclass(frozen=True)
class PauseTransition:
    """One durable pause/resume record.

    Appended to the pause journal so the *history* of pauses survives the
    process that made them — the gap that made the most recent incidents
    reconstructible only from raw logs.
    """

    at: datetime
    paused: bool
    reason: PauseReason | None
    actor: PauseActor
    detail: str = ""
    previous_reason: PauseReason | None = None
    held_seconds: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "at": _as_utc(self.at).isoformat(),
            "paused": self.paused,
            "reason": str(self.reason) if self.reason is not None else None,
            "actor": str(self.actor),
            "detail": self.detail,
            "previous_reason": (
                str(self.previous_reason) if self.previous_reason is not None else None
            ),
            "held_seconds": self.held_seconds,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PauseTransition":
        reason = data.get("reason")
        previous = data.get("previous_reason")
        return cls(
            at=datetime.fromisoformat(data["at"]),
            paused=bool(data["paused"]),
            reason=PauseReason(reason) if reason else None,
            actor=PauseActor(data["actor"]),
            detail=str(data.get("detail", "")),
            previous_reason=PauseReason(previous) if previous else None,
            held_seconds=data.get("held_seconds"),
        )


def _as_utc(value: datetime) -> datetime:
    """Normalize to aware UTC so journal rows and deltas are comparable."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
