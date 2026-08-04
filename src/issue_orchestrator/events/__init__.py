"""Event catalog and context for structured event emission.

This module provides:
- EventName: Canonical event name constants
- EventContext: Run/tick context for event payloads
- Helpers for building consistent event payloads
"""

from .catalog import EventName
from .context import EventContext
from .sse_envelope import SSE_SCHEMA_FIELD, SseEvent, apply_sse_envelope
from .stream import EventHub, SequencedEventSink, StreamEvent, EventSubscription

__all__ = [
    "EventName",
    "EventContext",
    "SSE_SCHEMA_FIELD",
    "SseEvent",
    "apply_sse_envelope",
    "EventHub",
    "SequencedEventSink",
    "StreamEvent",
    "EventSubscription",
]
