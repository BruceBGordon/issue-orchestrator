"""The public SSE envelope — the one owner that stamps the schema version.

``docs/user/stability.md`` publishes the SSE event stream as the project's only
``Versioned`` surface: a consumer can read ``schema`` off an event and refuse a
version it does not understand instead of misparsing it. That promise is only
true if *every* event carries the field, and it cannot be true if it depends on
each producer remembering to call :meth:`EventContext.enrich`.

So the envelope is applied here, at the serialization boundary every SSE event
passes through (``entrypoints.web.broadcast_event``), rather than at the many
places events are emitted. Raw ``dict`` emitters — the observer, direct
``broadcast_event`` calls from the web entrypoint and the E2E control routes —
get the same envelope as enriched ones, because none of them can skip this
function.

``run_id`` and ``tick_id`` are deliberately **not** stamped here. They identify
an orchestrator run and control tick, which a direct broadcast such as
``startup_complete`` has no meaningful value for, and inventing one would be
worse than omitting it. They are added by :meth:`EventContext.enrich` on
control-loop events, and the stability doc scopes the promise accordingly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .catalog import EVENT_SCHEMA_VERSION

#: Payload key carrying the public schema version.
SSE_SCHEMA_FIELD = "schema"

__all__ = ["SSE_SCHEMA_FIELD", "SseEvent", "apply_sse_envelope"]


@dataclass(frozen=True)
class SseEvent:
    """One enveloped event, ready for the SSE transport to serialize.

    A typed value object rather than a loose dict so the envelope is a contract
    the type system can see: ``data`` is guaranteed to carry
    :data:`SSE_SCHEMA_FIELD` because :func:`apply_sse_envelope` is the only
    place that constructs one.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def schema_version(self) -> int:
        """The envelope version this event was published with."""
        return self.data[SSE_SCHEMA_FIELD]


def apply_sse_envelope(event_type: str, data: Mapping[str, Any] | None) -> SseEvent:
    """Return an :class:`SseEvent` with the public envelope applied.

    Args:
        event_type: Event name, e.g. ``"session.started"``.
        data: Payload fields. ``None`` is treated as an empty payload.

    Returns:
        The event with ``schema`` stamped into its payload. The caller's mapping
        is copied, never mutated.

    Raises:
        ValueError: If the payload already declares a *different* schema
            version. There is exactly one live version, so a mismatch means a
            producer is hand-writing the field — a bug worth failing on rather
            than silently overwriting and shipping a mislabelled event.
    """
    payload = dict(data or {})
    declared = payload.get(SSE_SCHEMA_FIELD)
    if declared is not None and declared != EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"SSE event {event_type!r} declares {SSE_SCHEMA_FIELD}={declared!r}, "
            f"but the current envelope version is {EVENT_SCHEMA_VERSION!r}. "
            "Producers must not set the envelope version themselves."
        )
    payload[SSE_SCHEMA_FIELD] = EVENT_SCHEMA_VERSION
    return SseEvent(type=event_type, data=payload)
