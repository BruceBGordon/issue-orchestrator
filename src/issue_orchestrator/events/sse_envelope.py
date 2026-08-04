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
from types import MappingProxyType
from typing import Any

from .catalog import EVENT_SCHEMA_VERSION

#: Payload key carrying the public schema version on the wire.
SSE_SCHEMA_FIELD = "schema"

__all__ = ["SSE_SCHEMA_FIELD", "SseEvent", "apply_sse_envelope"]


@dataclass(frozen=True)
class SseEvent:
    """One enveloped event, ready for the SSE transport to serialize.

    The invariant this type exists to guarantee — a version on every event — is
    encoded rather than described:

    - ``schema_version`` is a required field with no default, so an event
      cannot be constructed without one.
    - It is validated against :data:`EVENT_SCHEMA_VERSION`, so it cannot carry a
      version the project does not publish.
    - ``payload`` is copied into a read-only mapping at construction, so a
      caller cannot mutate an event after the fact.
    - :attr:`data` composes the wire payload from both, so the version cannot be
      removed by editing a dictionary.

    Direct construction is therefore safe; there is no invalid state to reach.
    :func:`apply_sse_envelope` remains the intended entry point because it also
    rejects producer-declared versions.
    """

    type: str
    schema_version: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"SSE event {self.type!r} declares schema version "
                f"{self.schema_version!r}, but the published envelope version is "
                f"{EVENT_SCHEMA_VERSION!r}."
            )
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def data(self) -> Mapping[str, Any]:
        """The wire payload: producer fields plus the envelope version.

        A fresh read-only mapping each call, so no caller can strip the version
        out of a shared object.
        """
        return MappingProxyType(
            {**self.payload, SSE_SCHEMA_FIELD: self.schema_version}
        )


def apply_sse_envelope(event_type: str, data: Mapping[str, Any] | None) -> SseEvent:
    """Return an :class:`SseEvent` with the public envelope applied.

    Args:
        event_type: Event name, e.g. ``"session.started"``.
        data: Payload fields. ``None`` is treated as an empty payload.

    Returns:
        The event carrying the current envelope version. The caller's mapping is
        copied, never mutated.

    Raises:
        ValueError: If the payload already declares a *different* schema
            version. There is exactly one live version, so a mismatch means a
            producer is hand-writing the field — a bug worth failing on rather
            than silently overwriting and shipping a mislabelled event.
    """
    payload = dict(data or {})
    declared = payload.pop(SSE_SCHEMA_FIELD, None)
    if declared is not None and declared != EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"SSE event {event_type!r} declares {SSE_SCHEMA_FIELD}={declared!r}, "
            f"but the current envelope version is {EVENT_SCHEMA_VERSION!r}. "
            "Producers must not set the envelope version themselves."
        )
    return SseEvent(
        type=event_type,
        schema_version=EVENT_SCHEMA_VERSION,
        payload=payload,
    )
