"""Unit tests for the FastAPI web module."""

import base64
import json
from types import SimpleNamespace
import pytest
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch, Mock, AsyncMock
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import (
    app,
    get_orchestrator,
    set_client_host,
    set_orchestrator,
    set_server,
)
from issue_orchestrator.contracts.public import ShutdownRequestedPayload, StartupCompletePayload
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.execution.client_host import (
    ClientHostActionResult,
    ClientHostCapabilities,
    UnsupportedClientHost,
)


@pytest.fixture(autouse=True)
def prevent_os_exit():
    """Prevent shutdown_manager.exit() from calling os._exit().

    This is needed for pytest-xdist parallel test execution. The web module's
    shutdown_manager.exit() calls os._exit() which would crash the test worker
    and cause unrelated tests to be marked as failed.
    """
    with patch("issue_orchestrator.entrypoints.web.shutdown_manager.exit"):
        yield


@pytest.fixture(autouse=True)
def reset_client_host():
    """Reset client host between tests to avoid cross-test leakage."""
    set_client_host(UnsupportedClientHost())
    yield
    set_client_host(UnsupportedClientHost())


from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionHistoryEntry,
    PendingReview,
    PendingRework,
    DiscoveredReview,
    DiscoveredRework,
    DiscoveredFailure,
    PendingValidationRetry,
    ImmediateCleanup,
    OrchestratorState,
    AgentConfig,
    SessionStatus,
)
from issue_orchestrator.domain.dependency_gates import DependencyGateSnapshot
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.event_taxonomy import infer_event_intent
from issue_orchestrator.timeline import (
    TIMELINE_SCHEMA_VERSION,
    TimelineArtifact,
    TimelineEvent,
    TimelineStream,
)
from issue_orchestrator.events import EventName
from tests.unit.session_run_helpers import make_session_run_assets

_TEST_RUN_DIR_BY_ISSUE: dict[int, str] = {}


class _StubClientHost:
    def __init__(
        self,
        *,
        open_path_result: ClientHostActionResult | None = None,
        reveal_worktree_result: ClientHostActionResult | None = None,
        capabilities: ClientHostCapabilities | None = None,
    ) -> None:
        self._open_path_result = open_path_result or ClientHostActionResult(
            path="/tmp/path",
            action="opened",
        )
        self._reveal_worktree_result = reveal_worktree_result or ClientHostActionResult(
            path="/tmp/worktree",
            action="opened",
        )
        self._capabilities = capabilities or ClientHostCapabilities(
            open_path=True,
            reveal_worktree=True,
        )

    def capabilities(self) -> ClientHostCapabilities:
        return self._capabilities

    def open_path(self, path: Path) -> ClientHostActionResult:
        return ClientHostActionResult(
            path=str(path),
            action=self._open_path_result.action,
            message=self._open_path_result.message,
        )

    def reveal_worktree(self, path: Path) -> ClientHostActionResult:
        return ClientHostActionResult(
            path=str(path),
            action=self._reveal_worktree_result.action,
            message=self._reveal_worktree_result.message,
        )


def _ensure_test_run_dir(issue_number: int) -> str:
    """Create a real run dir with required artifacts for strict action wiring."""
    existing = _TEST_RUN_DIR_BY_ISSUE.get(issue_number)
    if existing:
        return existing

    from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

    root = Path(tempfile.mkdtemp(prefix=f"io-test-run-{issue_number}-"))
    worktree = root / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, f"issue-{issue_number}", issue_number=issue_number)
    (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
    claude_log = run.run_dir / "claude.jsonl"
    claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
    session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
    _TEST_RUN_DIR_BY_ISSUE[issue_number] = str(run.run_dir)
    return _TEST_RUN_DIR_BY_ISSUE[issue_number]


# Helper functions
def create_mock_orchestrator():
    """Create a mock orchestrator for testing."""
    mock_orch = MagicMock()

    # Create a real config object
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.queue_refresh_seconds = 600
    config.ui_mode = "web"
    config.web_port = 8080
    config.filtering.label = None
    config.filtering.milestone = None
    config.config_path = Path("/tmp/config.yaml")
    config.repo_root = Path("/tmp/repo")
    config.worktree_base = Path("/tmp/worktrees")  # Top-level worktree_base

    # Add a sample agent config
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )
    config.agents = {"agent:web": agent_config}

    # Create a real state object
    state = OrchestratorState(
        active_sessions=[],
        session_history=[],
        completed_today=[],
        paused=False,
        priority_queue=[],
        startup_status="complete",  # Required for dashboard to show issues
    )

    mock_orch.config = config
    mock_orch.state = state
    mock_orch.pause = MagicMock()
    mock_orch.resume = MagicMock()
    mock_orch.request_shutdown = MagicMock()
    mock_orch.shutdown_requested = False  # Public property for JSON serialization

    mock_deps = MagicMock()
    mock_deps.publish_recovery = MagicMock()
    mock_deps.publish_recovery.can_retry_publish.return_value = False
    mock_deps.timeline_reader = MagicMock()
    mock_orch.deps = mock_deps
    mock_orch.scheduler = MagicMock()
    mock_orch.scheduler.sort_by_priority.side_effect = lambda issues: issues
    mock_orch.scheduler.dependency_evaluator = None
    mock_orch.repository_host = MagicMock()
    mock_orch.repository_host.get_issue.return_value = None
    mock_orch.repository_host.update_label_cache = MagicMock()

    return mock_orch


def create_issue(number, title="Test Issue", labels=None):
    """Helper to create Issue objects for testing."""
    if labels is None:
        labels = ["agent:web"]
    return Issue(
        number=number,
        title=title,
        labels=labels,
    )


def create_session(issue, worktree_path="/tmp/worktree-1", branch_name="feature/issue-1"):
    """Helper to create Session objects for testing."""
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(worktree_path),
        branch_name=branch_name,
        run_assets=make_session_run_assets(
            Path(worktree_path),
            session_name=f"issue-{issue.number}",
        ),
    )


def build_timeline_event(
    event_name: str,
    *,
    issue_number: int = 123,
    event_id: str = "e1",
    timestamp: str = "2026-02-06T00:00:00Z",
    phase: str = "in_progress",
    step: str | None = None,
    status: str = "started",
    level: str = "phase",
    summary: str | None = None,
    parent_key: str | None = None,
    artifacts: list[TimelineArtifact] | None = None,
    detail: str | None = None,
    run_id: str | None = None,
    run_dir: str | None = None,
    agent: str | None = None,
    task: str | None = None,
    rework_cycle: int | None = None,
    reviewer_agent: str | None = None,
    logical_run: int = 1,
    logical_cycle: int | None = None,
    logical_phase: str | None = None,
    timeline_schema_version: int = TIMELINE_SCHEMA_VERSION,
    views: list[str] | None = None,
) -> TimelineEvent:
    """Build a TimelineEvent with sensible defaults for intent-focused tests.

    ``views`` mirrors the tags the TimelineWriter copies from
    ``events/view_registry.py``.  Leaving it ``None`` reproduces pre-registry
    data, which every timeline view includes; pass it explicitly (e.g.
    ``["ops", "debug"]``) to exercise view filtering.
    """
    from issue_orchestrator.entrypoints.web import _timeline_event_requires_run_dir

    inferred_intent = infer_event_intent(event_name=event_name, task=task)
    inferred_phase = logical_phase
    if inferred_phase is None:
        if inferred_intent.value == "review":
            inferred_phase = "review"
        elif inferred_intent.value == "rework":
            inferred_phase = "rework"
        elif inferred_intent.value == "orchestrator":
            inferred_phase = "orchestrator"
        else:
            inferred_phase = "coding"
    if logical_cycle is None:
        logical_cycle = (rework_cycle + 1) if isinstance(rework_cycle, int) and rework_cycle >= 0 else 1
    if run_dir is None and _timeline_event_requires_run_dir({"event": event_name, "review_oriented": inferred_intent.value == "review"}):
        run_dir = _ensure_test_run_dir(issue_number)

    return TimelineEvent(
        event_id=event_id,
        timestamp=timestamp,
        event=event_name,
        issue_number=issue_number,
        phase=phase,
        step=step or event_name.split(".")[-1],
        status=status,
        level=level,
        summary=summary,
        parent_key=parent_key or f"session:issue-{issue_number}",
        artifacts=artifacts or [],
        detail=detail,
        run_id=run_id,
        run_dir=run_dir,
        agent=agent,
        task=task,
        rework_cycle=rework_cycle,
        reviewer_agent=reviewer_agent,
        timeline_schema_version=timeline_schema_version,
        review_oriented=(inferred_intent.value == "review"),
        event_intent=inferred_intent.value,
        logical_run=logical_run,
        logical_cycle=logical_cycle,
        logical_phase=inferred_phase,
        views=views,
    )


def fetch_issue_detail_payload(
    events: list[TimelineEvent],
    *,
    issue_number: int = 123,
    title: str = "Detail Issue",
    can_retry_publish: bool = False,
    dependency_gate_snapshot: DependencyGateSnapshot | None = None,
) -> dict[str, Any]:
    """Call /api/issue-detail with a mocked timeline and return JSON payload."""
    mock_orch = create_mock_orchestrator()
    mock_orch.state.cached_queue_issues = [create_issue(issue_number, title)]
    if dependency_gate_snapshot is not None:
        mock_orch.state.dependency_gate_snapshot = dependency_gate_snapshot
    mock_orch.deps.publish_recovery.can_retry_publish.return_value = can_retry_publish
    mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
        issue_number=issue_number,
        events=events,
    )
    set_orchestrator(mock_orch)
    try:
        client = TestClient(app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        return response.json()
    finally:
        set_orchestrator(None)


def _latest_run(payload: dict[str, Any]) -> dict[str, Any]:
    runs = payload.get("runs")
    assert isinstance(runs, list) and runs
    latest = runs[-1]
    assert isinstance(latest, dict)
    return latest


def _first_cycle(payload: dict[str, Any]) -> dict[str, Any]:
    cycles = payload.get("cycles")
    assert isinstance(cycles, list) and cycles
    first = cycles[0]
    assert isinstance(first, dict)
    return first

