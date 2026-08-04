"""The public SSE envelope must be applied at the boundary, not by producers.

``docs/user/stability.md`` publishes the SSE stream as the project's only
``Versioned`` surface. That promise is only true if every event reaches the wire
carrying ``schema`` — including events from producers that never touch
``EventContext.enrich`` — so these tests exercise the boundary rather than the
helper.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from uuid import UUID

import pytest

from issue_orchestrator.entrypoints import web
from issue_orchestrator.entrypoints.web import (
    add_event_subscriber,
    broadcast_event,
    remove_event_subscriber,
)
from issue_orchestrator.events import EventContext
from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION
from issue_orchestrator.events.sse_envelope import (
    SSE_SCHEMA_FIELD,
    SseEvent,
    apply_sse_envelope,
)
from issue_orchestrator.execution.lifecycle_sse import LifecycleSSEPlugin


class TestApplySseEnvelope:
    """Unit behavior of the envelope owner itself."""

    def test_stamps_the_schema_version_on_a_raw_payload(self):
        event = apply_sse_envelope("session.started", {"issue_number": 42})

        assert event == SseEvent(
            type="session.started",
            schema_version=EVENT_SCHEMA_VERSION,
            payload={"issue_number": 42},
        )
        assert event.data == {
            "issue_number": 42,
            SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION,
        }

    def test_stamps_the_schema_version_on_an_empty_payload(self):
        event = apply_sse_envelope("startup_complete", None)

        assert event.data == {SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION}

    def test_preserves_context_fields_from_an_enriched_payload(self):
        run_id = UUID("00000000-0000-0000-0000-0000000000ab")
        enriched = EventContext(run_id=run_id, tick_id=9).enrich({"issue_number": 42})

        event = apply_sse_envelope("session.started", enriched)

        assert event.data == {
            SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION,
            "run_id": str(run_id),
            "tick_id": 9,
            "issue_number": 42,
        }

    def test_does_not_mutate_the_callers_payload(self):
        payload = {"issue_number": 42}

        apply_sse_envelope("session.started", payload)

        assert payload == {"issue_number": 42}

    def test_rejects_a_payload_declaring_a_different_schema_version(self):
        """One live version exists; a hand-written mismatch is a bug, not a hint."""
        with pytest.raises(ValueError, match="envelope version"):
            apply_sse_envelope(
                "session.started",
                {"issue_number": 42, SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION + 1},
            )

    def test_accepts_a_payload_redeclaring_the_current_version(self):
        """``EventContext.enrich()`` already sets it; that must not double-count."""
        event = apply_sse_envelope(
            "session.started",
            {"issue_number": 42, SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION},
        )

        assert event.data == {
            "issue_number": 42,
            SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION,
        }

    @pytest.mark.parametrize(
        "declared",
        [
            pytest.param(True, id="bool-true-equals-1-but-serializes-as-true"),
            pytest.param(1.0, id="float-equals-1-but-serializes-as-1.0"),
            pytest.param(None, id="explicit-none-is-a-declaration-not-absence"),
            pytest.param("1", id="string"),
            pytest.param(EVENT_SCHEMA_VERSION + 1, id="different-int"),
        ],
    )
    def test_rejects_any_producer_declared_version_that_is_not_the_published_one(
        self, declared
    ):
        """Equality alone is too weak on the only runtime-versioned surface.

        ``True`` and ``1.0`` compare equal to ``1`` but reach a consumer as JSON
        ``true`` and ``1.0``, which no client matching the published integer
        would recognize. An explicit ``None`` is a declaration of something
        invalid, not an absent field, so it must fail rather than be replaced.
        """
        with pytest.raises(ValueError, match="envelope version"):
            apply_sse_envelope(
                "session.started",
                {"issue_number": 42, SSE_SCHEMA_FIELD: declared},
            )

    def test_accepted_wire_version_is_the_canonical_integer(self):
        event = apply_sse_envelope("session.started", {"issue_number": 42})
        wire_version = event.data[SSE_SCHEMA_FIELD]

        assert type(wire_version) is int
        assert wire_version == EVENT_SCHEMA_VERSION


class TestSseEventInvariant:
    """Invalid envelope states must be unrepresentable, not merely undocumented."""

    def test_cannot_be_constructed_without_a_schema_version(self):
        with pytest.raises(TypeError):
            SseEvent(type="session.started")

    @pytest.mark.parametrize(
        "schema_version",
        [
            pytest.param(True, id="bool-true-equals-1-but-serializes-as-true"),
            pytest.param(1.0, id="float-equals-1-but-serializes-as-1.0"),
            pytest.param(None, id="none"),
            pytest.param("1", id="string"),
            pytest.param(EVENT_SCHEMA_VERSION + 1, id="different-int"),
        ],
    )
    def test_cannot_be_constructed_with_an_unpublished_schema_version(
        self, schema_version
    ):
        """Exact type and value - the exported owner must not admit look-alikes.

        ``SseEvent`` is exported from ``events``, so direct construction is part
        of the surface. Accepting ``True`` here would put JSON ``true`` on the
        stream this project calls its only runtime-versioned contract.
        """
        with pytest.raises(ValueError, match="published envelope version"):
            SseEvent(
                type="session.started",
                schema_version=schema_version,
                payload={"issue_number": 42},
            )

    def test_constructed_wire_version_is_the_canonical_integer(self):
        event = SseEvent(
            type="session.started", schema_version=EVENT_SCHEMA_VERSION
        )

        assert type(event.data[SSE_SCHEMA_FIELD]) is int

    def test_data_always_carries_the_version_even_for_an_empty_payload(self):
        event = SseEvent(type="orchestrator.paused", schema_version=EVENT_SCHEMA_VERSION)

        assert event.data == {SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION}

    def test_stored_payload_cannot_be_mutated_after_construction(self):
        source = {"issue_number": 42}
        event = SseEvent(
            type="session.started",
            schema_version=EVENT_SCHEMA_VERSION,
            payload=source,
        )

        with pytest.raises(TypeError):
            event.payload["issue_number"] = 99
        source["issue_number"] = 7  # the constructor copied, so this is inert

        assert event.data["issue_number"] == 42

    def test_mutating_the_wire_payload_cannot_strip_the_version(self):
        event = apply_sse_envelope("session.started", {"issue_number": 42})

        with pytest.raises(TypeError):
            del event.data[SSE_SCHEMA_FIELD]

        assert event.data[SSE_SCHEMA_FIELD] == EVENT_SCHEMA_VERSION


class TestBoundaryAppliesEnvelopeForEveryProducer:
    """Producer-to-SSE-output coverage for both paths into the stream."""

    @pytest.mark.asyncio
    async def test_raw_trace_event_producer_reaches_the_wire_versioned(
        self, monkeypatch
    ):
        """A producer that never calls ``enrich`` still yields a versioned event.

        This is the path the observer uses: ``events.publish(TraceEvent(...))``
        with a plain dict, through the pluggy hook, into the SSE broadcast.

        ``on_trace_event`` schedules the broadcast as a task, so readiness is
        signalled explicitly by an ``asyncio.Event`` set by a wrapper around the
        real ``broadcast_event``. No sleeping or yield-counting: the wait ends
        when the broadcast has actually happened.
        """
        broadcast_finished = asyncio.Event()
        real_broadcast = web.broadcast_event

        async def signalling_broadcast(event_type, data=None):
            try:
                await real_broadcast(event_type, data)
            finally:
                broadcast_finished.set()

        monkeypatch.setattr(web, "broadcast_event", signalling_broadcast)

        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(queue)
        try:
            LifecycleSSEPlugin().on_trace_event(
                "observation.completion_detected",
                {"issue_number": 42, "outcome": "completed"},
            )
            await broadcast_finished.wait()

            event = queue.get_nowait()
        finally:
            remove_event_subscriber(queue)

        assert event["type"] == "observation.completion_detected"
        assert event["data"] == {
            "issue_number": 42,
            "outcome": "completed",
            SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION,
        }

    @pytest.mark.asyncio
    async def test_direct_broadcast_reaches_the_wire_versioned(self):
        """``startup_complete`` is broadcast directly, bypassing trace events."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(queue)
        try:
            await broadcast_event("startup_complete", {"elapsed_seconds": 1.5})

            event = queue.get_nowait()
        finally:
            remove_event_subscriber(queue)

        assert event["data"] == {
            "elapsed_seconds": 1.5,
            SSE_SCHEMA_FIELD: EVENT_SCHEMA_VERSION,
        }

    @pytest.mark.asyncio
    async def test_enriched_producer_keeps_run_and_tick_context(self):
        run_id = UUID("00000000-0000-0000-0000-0000000000cd")
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(queue)
        try:
            await broadcast_event(
                "session.started",
                EventContext(run_id=run_id, tick_id=3).enrich({"issue_number": 7}),
            )

            event = queue.get_nowait()
        finally:
            remove_event_subscriber(queue)

        assert event["data"]["run_id"] == str(run_id)
        assert event["data"]["tick_id"] == 3
        assert event["data"][SSE_SCHEMA_FIELD] == EVENT_SCHEMA_VERSION


def test_broadcast_event_is_the_only_way_to_enqueue_an_sse_event():
    """No producer may bypass the envelope owner.

    The envelope is only a guarantee if every event reaches subscriber queues
    through ``broadcast_event``. If another function starts enqueueing directly,
    the promise silently becomes conditional again - so fail here instead.
    """
    module_source = Path(inspect.getfile(web)).read_text(encoding="utf-8")
    tree = ast.parse(module_source)

    enqueuing_functions = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "put_nowait"
            ):
                enqueuing_functions.add(node.name)

    assert enqueuing_functions == {"broadcast_event"}, (
        "Only broadcast_event may enqueue SSE events, because it is where the "
        f"public envelope is applied. Also enqueueing: {sorted(enqueuing_functions)}."
    )
