"""SSE and issue-row web route tests split from test_web."""

# ruff: noqa: F403,F405,SLF001

from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from tests.unit import test_web as _support
from tests.unit.route_helpers import iter_route_paths
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestGetTemplates:
    """Test the get_templates helper function."""

    def test_get_templates_returns_jinja_environment(self):
        """Test that get_templates returns a Jinja2 Environment."""
        from issue_orchestrator.entrypoints.web_templates import get_templates
        from jinja2 import Environment

        env = get_templates()
        assert isinstance(env, Environment)
        assert env.autoescape("dashboard.html") is True


class TestDashboardReadModelLoggingHelpers:
    """Test dashboard read-model diagnostic helper behavior."""

    def test_reset_retry_pending_issue_numbers_uses_label_manager_names(self):
        from types import SimpleNamespace

        from issue_orchestrator.entrypoints.web_read_model_routes import (
            _reset_retry_pending_issue_numbers,
        )

        view_model = SimpleNamespace(
            queue_items=[
                {
                    "issue_number": 11,
                    "orchestrator_labels": ["custom-reset-retry-pending"],
                },
                {
                    "issue_number": 12,
                    "orchestrator_labels": ["other-label"],
                },
            ],
            blocked_items=[
                {
                    "issue_number": 13,
                    "orchestrator_labels": ["custom-reset-retry-scratch-pending"],
                }
            ],
            awaiting_merge_items=[
                {
                    "issue_number": "not-int",
                    "orchestrator_labels": ["custom-reset-retry-pending"],
                }
            ],
            active_items=[],
            completed_items=[],
        )
        orchestrator = SimpleNamespace(
            deps=SimpleNamespace(
                label_manager=SimpleNamespace(
                    reset_retry_pending="custom-reset-retry-pending",
                    reset_retry_scratch_pending="custom-reset-retry-scratch-pending",
                )
            )
        )

        assert _reset_retry_pending_issue_numbers(view_model, orchestrator) == [11, 13]

    def test_reset_retry_pending_issue_numbers_requires_complete_label_manager(self):
        from types import SimpleNamespace

        from issue_orchestrator.entrypoints.web_read_model_routes import (
            _reset_retry_pending_issue_numbers,
        )

        view_model = SimpleNamespace(
            queue_items=[],
            blocked_items=[],
            awaiting_merge_items=[],
            active_items=[],
            completed_items=[],
        )
        orchestrator = SimpleNamespace(
            deps=SimpleNamespace(
                label_manager=SimpleNamespace(
                    reset_retry_pending="custom-reset-retry-pending",
                )
            )
        )

        with pytest.raises(AttributeError):
            _reset_retry_pending_issue_numbers(view_model, orchestrator)


class TestSSEFunctionality:
    """Test Server-Sent Events functionality."""

    @pytest.mark.asyncio
    async def test_broadcast_event_to_subscribers(self):
        """Test broadcasting events to subscribers."""
        import asyncio
        from issue_orchestrator.entrypoints.web import add_event_subscriber, broadcast_event, remove_event_subscriber

        # Create a test queue and add it as a subscriber
        test_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(test_queue)

        try:
            # Broadcast an event
            await broadcast_event("test_event", {"key": "value"})

            # Check the queue received the event
            assert not test_queue.empty()
            event = test_queue.get_nowait()
            assert event["type"] == "test_event"
            assert event["data"] == {"key": "value", "schema": EVENT_SCHEMA_VERSION}
        finally:
            remove_event_subscriber(test_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_handles_empty_data(self):
        """Test broadcasting events with no data."""
        import asyncio
        from issue_orchestrator.entrypoints.web import add_event_subscriber, broadcast_event, remove_event_subscriber

        test_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(test_queue)

        try:
            await broadcast_event("empty_event")

            event = test_queue.get_nowait()
            assert event["type"] == "empty_event"
            assert event["data"] == {"schema": EVENT_SCHEMA_VERSION}
        finally:
            remove_event_subscriber(test_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_removes_full_queues(self):
        """Test that full queues are removed from subscribers."""
        import asyncio
        from issue_orchestrator.entrypoints.web import add_event_subscriber, broadcast_event, event_subscribers_snapshot, remove_event_subscriber

        # Create a queue with size 1 and fill it
        full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"dummy": "event"})

        add_event_subscriber(full_queue)
        assert full_queue in event_subscribers_snapshot()

        try:
            # This should fail silently and remove the full queue
            await broadcast_event("overflow_event")

            # Queue should be removed from subscribers
            assert full_queue not in event_subscribers_snapshot()
        finally:
            remove_event_subscriber(full_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_no_subscribers(self):
        """Test broadcasting when there are no subscribers."""
        from issue_orchestrator.entrypoints.web import broadcast_event, event_subscribers_snapshot, swapped_event_subscribers

        # Ensure no subscribers
        original_subscribers = event_subscribers_snapshot()
        with swapped_event_subscribers(set()):
            # Should not raise any errors
            await broadcast_event("no_listeners", {"data": "test"})
        assert event_subscribers_snapshot() == original_subscribers

    def test_events_endpoint_exists(self):
        """Test that /api/events endpoint is registered."""
        from issue_orchestrator.entrypoints.web import app

        # Check the endpoint is registered by looking at routes
        routes = list(iter_route_paths(app))
        assert "/api/events" in routes

    @pytest.mark.asyncio
    async def test_shutdown_endpoint_broadcasts_event(self, monkeypatch):
        """Shutdown endpoint should emit shutdown_requested SSE event."""
        import asyncio
        from types import SimpleNamespace
        from issue_orchestrator.entrypoints import web, web_operator_routes
        from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator

        class OrchestratorStub:
            def __init__(self):
                self.state = SimpleNamespace(active_sessions=[SimpleNamespace(issue=SimpleNamespace(number=1))])
                self.shutdown_called = False

            def request_shutdown(self, force: bool = False) -> None:
                self.shutdown_called = True

        orchestrator = OrchestratorStub()
        original = get_orchestrator()
        set_orchestrator(orchestrator)

        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        web.add_event_subscriber(queue)

        operator_deps = web_operator_routes.WebOperatorDependencies(
            get_client_host=lambda: _StubClientHost(),
            broadcast_event=web.broadcast_event,
            trigger_server_shutdown=lambda: None,
        )
        monkeypatch.setattr(web_operator_routes.shutdown_manager, "request_shutdown", lambda reason: None)
        monkeypatch.setattr(web_operator_routes.shutdown_manager, "exit", lambda: None)

        try:
            # Build a Request stub the new endpoint can read JSON
            # body from. We can't import starlette's Request and
            # construct one cheaply, so use the SimpleNamespace +
            # async ``json()`` helper pattern.
            class _RequestStub:
                async def json(self):
                    return {"reason": "sse test", "actor": "unit-test"}

            response = await web_operator_routes.shutdown(
                _RequestStub(),  # type: ignore[arg-type]
                orchestrator,
                operator_deps,
                force=False,
            )
            assert response.status_code == 200
            event = queue.get_nowait()
            assert event["type"] == "shutdown_requested"
            assert event["data"]["force"] is False
            assert event["data"]["reason"] == "sse test"
            assert orchestrator.shutdown_called is True
            ShutdownRequestedPayload.model_validate(event["data"])
        finally:
            web.remove_event_subscriber(queue)
            set_orchestrator(original)

    @pytest.mark.asyncio
    async def test_startup_complete_broadcasts_event(self, monkeypatch, tmp_path):
        """Startup path should emit startup_complete event for the UI."""
        import asyncio
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.infra.config import Config

        startup_event = asyncio.Event()
        captured: dict = {}

        async def fake_broadcast_event(event_type: str, data: dict | None = None) -> None:
            if event_type == "startup_complete":
                captured["event_type"] = event_type
                captured["data"] = data or {}
                startup_event.set()

        async def fake_run_web_dashboard(
            orchestrator,
            port: int,
            open_browser: bool = True,
            on_server_started=None,
            server_published=None,
        ) -> None:
            # The real implementation sets this once the server has
            # published its bound port; orchestration waits on it before
            # launching anything (#6924 F7). A stub that never sets it
            # would stall startup for the full publish timeout.
            if server_published is not None:
                server_published.set()
            await startup_event.wait()

        async def fast_sleep(_seconds: float) -> None:
            return None

        class OrchestratorStub:
            def __init__(self, root: Path):
                self.config = Config()
                self.config.repo_root = root
                self.shutdown_requested = False

            async def startup(self) -> None:
                return None

            async def run_loop(self) -> None:
                return None

            def close(self) -> None:
                return None

        monkeypatch.setattr(web, "broadcast_event", fake_broadcast_event)
        monkeypatch.setattr(web, "run_web_dashboard", fake_run_web_dashboard)
        monkeypatch.setattr(web.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(web.shutdown_manager, "initialize", lambda _: None)
        monkeypatch.setattr(web.shutdown_manager, "request_shutdown", lambda reason: None)
        monkeypatch.setattr(web.shutdown_manager, "exit", lambda: None)

        orchestrator = OrchestratorStub(tmp_path)
        await web.run_with_web_dashboard(orchestrator, port=0, open_browser=False)

        assert captured["event_type"] == "startup_complete"
        assert "elapsed_seconds" in captured["data"]
        StartupCompletePayload.model_validate(captured["data"])


class TestEmitEventHelper:
    """Test the trace event emission via PluginManager.emit()."""

    def test_plugin_manager_emit_broadcasts_to_hooks(self):
        """Test that PluginManager.emit() broadcasts to on_trace_event hooks."""
        from issue_orchestrator.execution.manager import PluginManager
        from issue_orchestrator.infra.hooks.hookspec import hookimpl

        # Create a test plugin that captures events
        events_received = []

        class TestPlugin:
            @hookimpl
            def on_trace_event(self, event: str, data: dict) -> None:
                events_received.append((event, data))

        # Create plugin manager and register test plugin
        pm = PluginManager(terminal_plugin="subprocess")
        pm.register_plugin(TestPlugin(), name="test_plugin")

        # Emit an event
        pm.emit("test.event", {"key": "value"})

        # Verify event was received
        assert len(events_received) == 1
        assert events_received[0] == ("test.event", {"key": "value"})


class TestSSEEventStreamFormat:
    """Tests for SSE event stream formatting."""

    @pytest.mark.asyncio
    async def test_events_stream_formats_event_and_data(self):
        """Ensure /api/events emits event and data lines with JSON payload."""
        import json
        import asyncio
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import broadcast_event

        class NotifyingSet(set):
            def __init__(self, event):
                super().__init__()
                self._event = event

            def add(self, item):
                super().add(item)
                self._event.set()

        from issue_orchestrator.entrypoints.web import swapped_event_subscribers
        ready = asyncio.Event()

        class DummyRequest:
            def __init__(self):
                self.connected = True

            async def is_disconnected(self):
                return not self.connected

        with swapped_event_subscribers(NotifyingSet(ready)):
            request = DummyRequest()
            response = await web.events(request)
            iterator = response.body_iterator

            async def read_chunk():
                return await iterator.__anext__()

            read_task = asyncio.create_task(read_chunk())
            await ready.wait()
            await broadcast_event("session.started", {"issue_number": 123, "status": "active"})

            chunk = await read_task
            request.connected = False

            assert chunk["event"] == "session.started"
            payload = json.loads(chunk["data"])
            # The broadcast boundary stamps the public envelope, so a raw
            # producer's payload reaches the wire versioned (issue #6464).
            assert payload == {
                "issue_number": 123,
                "status": "active",
                "schema": EVENT_SCHEMA_VERSION,
            }


class TestIssueRowsEndpoint:
    """Tests for the issue row rendering endpoint."""

    def test_issue_rows_returns_rendered_html(self):
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator
        from issue_orchestrator.domain.models import Issue, OrchestratorState
        from issue_orchestrator.infra.config import Config

        class OrchestratorStub:
            def __init__(self):
                self.state = OrchestratorState(
                    startup_status="complete",
                    cached_queue_issues=[Issue(number=7, title="Test", labels=["agent:web"])],
                )
                self.config = Config()
                self.config.repo = "test/repo"
                self.config.repo_root = Path("/tmp/repo")
                self.shutdown_requested = False
                # The dashboard route resolves its circuit reader from this
                # required facade property (issue #5980 F2/A1); a stub must
                # supply one explicitly rather than default to "healthy".
                self.provider_circuit = NO_PROVIDER_CIRCUIT_STATUS

        original = get_orchestrator()
        set_orchestrator(OrchestratorStub())
        try:
            client = TestClient(web.app)
            response = client.get("/api/issue-rows?tab=queue")
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1
            assert "issue-row-group" in data["rows"][0]["html"]
        finally:
            set_orchestrator(original)

    def test_view_model_snapshot_returns_rows_from_same_snapshot(self):
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator
        from issue_orchestrator.domain.models import Issue, OrchestratorState
        from issue_orchestrator.infra.config import Config

        class OrchestratorStub:
            def __init__(self):
                self.state = OrchestratorState(
                    startup_status="complete",
                    cached_queue_issues=[Issue(number=11, title="Snapshot Test", labels=["agent:web"])],
                )
                self.config = Config()
                self.config.repo = "test/repo"
                self.config.repo_root = Path("/tmp/repo")
                self.shutdown_requested = False
                # The dashboard route resolves its circuit reader from this
                # required facade property (issue #5980 F2/A1); a stub must
                # supply one explicitly rather than default to "healthy".
                self.provider_circuit = NO_PROVIDER_CIRCUIT_STATUS

        original = get_orchestrator()
        set_orchestrator(OrchestratorStub())
        try:
            client = TestClient(web.app)
            response = client.get("/api/view-model-snapshot?tab=queue")
            assert response.status_code == 200
            data = response.json()
            assert "view_model" in data
            assert data["count"] == 1
            assert data["rows"][0]["issue_number"] == 11
            assert data["view_model"]["queue_count"] >= 0
        finally:
            set_orchestrator(original)

    def test_plugin_manager_emit_with_empty_data(self):
        """Test that emit() works with no data argument."""
        from issue_orchestrator.execution.manager import PluginManager
        from issue_orchestrator.infra.hooks.hookspec import hookimpl

        events_received = []

        class TestPlugin:
            @hookimpl
            def on_trace_event(self, event: str, data: dict) -> None:
                events_received.append((event, data))

        pm = PluginManager(terminal_plugin="subprocess")
        pm.register_plugin(TestPlugin(), name="test_plugin")

        # Emit without data
        pm.emit("test.event")

        assert len(events_received) == 1
        assert events_received[0] == ("test.event", {})
