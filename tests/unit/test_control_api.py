"""Unit tests for the control API module.

This test suite covers the behavior of HTTP endpoints in the control API,
focusing on:
- State transitions (pause/resume/shutdown)
- Request handling (refresh with inflight_stable_ids)
- Error responses when orchestrator is not initialized
- SSE event streaming behavior
- Snapshot generation

Testing strategy:
- Mock the orchestrator dependency at the module level
- Use FastAPI's TestClient for synchronous endpoint testing
- Test actual behavior, not implementation details
"""

import json
import os
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    control_app,
    get_supervisor,
    set_orchestrator,
    get_orchestrator,
    set_control_actions,
    set_supervisor,
)
from issue_orchestrator.entrypoints import control_api_shutdown_state
from issue_orchestrator.execution.control_center_actions import ActionResult, ControlCenterActions
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_guardrails import RepoGuardrailsError
from issue_orchestrator.infra.supervisor import (
    MultiInstanceStatus,
    SupervisorStatus,
)
from issue_orchestrator.execution.repository_engine_supervisor import (
    build_default_supervisor_ops,
)
from issue_orchestrator.ports.repository_engine_supervisor import SupervisorOps


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_CENTER_JS = REPO_ROOT / "src/issue_orchestrator/static/js/control_center.js"


# --- Fixtures ---


def create_mock_orchestrator():
    """Create a mock orchestrator with required attributes."""
    mock = MagicMock()

    # Create state with realistic defaults
    mock.state = OrchestratorState()

    # Mock methods that endpoints call
    mock.pause = MagicMock()
    mock.resume = MagicMock()
    mock.request_shutdown = MagicMock()
    mock.request_refresh = MagicMock()

    # Mock event_hub for SSE endpoints
    mock.event_hub = MagicMock()
    mock.event_hub.last_event_id = 0
    mock.event_hub.stats.return_value = {
        "subscribers": 0,
        "buffer_size": 0,
        "oldest_event_id": None,
        "newest_event_id": None,
    }

    # Mock config for snapshot endpoint
    mock.config = Config()
    mock.config.repo = "test/repo"

    # Mock deps for snapshot endpoint
    mock.deps = MagicMock()
    mock.deps.repository_host = MagicMock()
    mock.deps.services.instance_id = "test-instance-id"

    # Configure label_manager to return real strings (not MagicMock)
    lm = mock.deps.label_manager
    lm.blocked = "blocked"
    lm.needs_human = "blocked-needs-human"
    lm.tech_lead_needs_human = "tech-lead-needs-human"
    lm.blocked_failed = "blocked-failed"
    lm.blocked_pr_closed = "blocked:pr-closed"
    lm.pr_pending = "pr-pending"
    lm.in_progress = "in-progress"
    lm.get_blocking = MagicMock(
        side_effect=lambda labels: [
            label for label in labels
            if label in {"blocked", "blocked-needs-human", "blocked-failed"}
        ],
    )
    lm.is_pr_pending = MagicMock(side_effect=lambda labels: "pr-pending" in labels)

    # The shared needs-human block owner, faithful to production: it is the
    # ONE writer of that label, and it writes through the same repository host
    # the operator routes would otherwise have called directly (#6999 F2 r3).
    class _SharedBlock:
        def owns(self, label: str) -> bool:
            return label == lm.needs_human

        def force_clear(self, target: int, reason: str):
            from issue_orchestrator.control.needs_human_block import BlockOutcome

            del reason
            try:
                mock.repository_host.remove_label(target, lm.needs_human)
            except Exception:
                return BlockOutcome.FAILED
            return BlockOutcome.CLEARED

        def unsettleable_holders(self, issue_number: int):
            del issue_number
            return ()

    mock.deps.needs_human_block = _SharedBlock()

    # The REAL operator retry/dismiss command, composed through the REAL
    # bootstrap builder (#6999 F5). Endpoint tests must exercise the actual
    # transition - labels first, local state only if that committed - rather
    # than a mock that would report success for either order.
    class _FreshLabels:
        """Fresh reads, delegated exactly as ``build_orchestrator_for_testing``."""

        def read_issue_labels(self, issue_number: int) -> list[str]:
            return mock.repository_host.get_issue_labels(issue_number)

    def _compose_operator_commands():
        """Compose against the mock's CURRENT collaborators.

        Production composes once, at bootstrap. Tests rewire the repository
        host, the block owner and the queue-cache store per case, so the
        composition is deferred to call time here — the builder, the command
        and the transition under test are the real ones either way.
        """
        from issue_orchestrator.entrypoints.bootstrap_operator_commands import (
            build_operator_issue_command_factory,
        )

        mock.deps.operator_issue_command_factory = (
            build_operator_issue_command_factory(
                mock.config,
                repository_host=mock.repository_host,
                label_manager=lm,
                needs_human_block=mock.deps.needs_human_block,
                fresh_issue_reader=_FreshLabels(),
                queue_cache_store=mock.deps.queue_cache_store,
            )
        )
        return mock.deps.operator_issue_command_factory(
            state=lambda: mock.state,
            run_locked=lambda fn: fn(),
        )

    class _LiveOperatorCommands:
        def retry(self, issue_number: int):
            return _compose_operator_commands().retry(issue_number)

        def dismiss(self, issue_number: int):
            return _compose_operator_commands().dismiss(issue_number)

    mock.operator_issue_commands = _LiveOperatorCommands()

    # Mock event context for snapshot (use public property)
    mock.event_context = MagicMock()
    mock.event_context.tick_id = 0

    return mock


@pytest.fixture
def mock_orchestrator():
    """Fixture providing a mock orchestrator."""
    return create_mock_orchestrator()


@pytest.fixture
def client_with_orchestrator(mock_orchestrator):
    """Create a test client with the orchestrator set."""
    set_orchestrator(mock_orchestrator)
    try:
        yield TestClient(control_app), mock_orchestrator
    finally:
        set_orchestrator(None)


@pytest.fixture
def client_without_orchestrator():
    """Create a test client without an orchestrator."""
    set_orchestrator(None)
    return TestClient(control_app)


@pytest.fixture
def supervisor_client():
    """Create a test client for supervisor endpoints (no orchestrator needed)."""
    return TestClient(control_app)


@pytest.fixture
def mock_supervisor():
    """Inject a mock SupervisorOps into the control API."""
    mock = MagicMock(spec=SupervisorOps)
    mock.status.return_value = SupervisorStatus(state="stopped")
    mock.status_all_instances.return_value = MultiInstanceStatus(repo_root="", expected_count=1, instances=[])
    mock.stop.return_value = True
    mock.stop_by_port.return_value = True
    set_supervisor(mock)
    yield mock
    set_supervisor(build_default_supervisor_ops())


class TestStopRepoOrchestratorEndpoint:
    """``POST /api/repos/{repo_id}/stop`` must consume the shared reason gate.

    The reviewer flagged on PR #6263 that this route was parsing
    ``reason``/``actor`` itself instead of routing through
    ``parse_shutdown_reason()``, leaving the policy with two owners.
    These tests pin the route to the shared validator's behavior so a
    future drift would fail here, not in production.
    """

    def test_stop_repo_rejects_missing_reason(self, mock_supervisor):
        client = TestClient(control_app)

        response = client.post("/api/repos/test-repo/stop", json={})

        assert response.status_code == 400
        assert response.json()["error"] == "reason is required"
        mock_supervisor.stop.assert_not_called()

    def test_stop_repo_rejects_empty_reason(self, mock_supervisor):
        client = TestClient(control_app)

        response = client.post(
            "/api/repos/test-repo/stop",
            json={"reason": "   "},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "reason is required"
        mock_supervisor.stop.assert_not_called()

    def test_stop_repo_threads_reason_and_actor_into_supervisor(self, mock_supervisor):
        client = TestClient(control_app)

        response = client.post(
            "/api/repos/test-repo/stop",
            json={
                "reason": "operator stop via /api/repos endpoint",
                "actor": "test-control-api",
                "force": False,
            },
        )

        assert response.status_code == 200
        mock_supervisor.stop.assert_called_once()
        kwargs = mock_supervisor.stop.call_args.kwargs
        assert kwargs["reason"] == "operator stop via /api/repos endpoint"
        assert kwargs["actor"] == "test-control-api"
        assert kwargs["force"] is False

    def test_stop_repo_default_actor_when_omitted(self, mock_supervisor):
        client = TestClient(control_app)

        response = client.post(
            "/api/repos/test-repo/stop",
            json={"reason": "explicit reason without actor"},
        )

        assert response.status_code == 200
        kwargs = mock_supervisor.stop.call_args.kwargs
        assert kwargs["actor"] == "control_api.stop_repo"
