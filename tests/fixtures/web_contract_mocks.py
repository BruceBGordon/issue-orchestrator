"""Shared mocks for web contract tests."""

from pathlib import Path

from issue_orchestrator.domain.models import AgentConfig, OrchestratorState
from issue_orchestrator.events import EventHub
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import (
    NO_PROVIDER_CIRCUIT_STATUS,
    ProviderCircuitStatusReader,
)
from issue_orchestrator.domain.pause_state import PauseState
from issue_orchestrator.ports.tech_lead_run_record_store import (
    NO_TECH_LEAD_RUN_HISTORY,
    TechLeadRunHistoryReader,
)
from tests.conftest import operator_paused_state


class MockOrchestratorForWeb:
    """Minimal orchestrator mock that satisfies the web contract protocol."""

    # Explicit, and required by the real facade: the dashboard route resolves
    # its provider circuit-status reader from this property, and a mock that
    # silently defaulted would let missing wiring read as a healthy provider
    # fleet (issue #5980 F2/A1). Override per test to simulate an outage.
    provider_circuit: ProviderCircuitStatusReader = NO_PROVIDER_CIRCUIT_STATUS

    # Same rule for the local tech-lead run history the dashboard reads (ADR-0033
    # / #6858): the route goes through the engine's required facade property, so
    # a mock without one makes ``GET /`` raise instead of rendering. Override per
    # test to publish runs.
    tech_lead_run_history: TechLeadRunHistoryReader = NO_TECH_LEAD_RUN_HISTORY

    def __init__(self) -> None:
        self.state = OrchestratorState(
            active_sessions=[],
            session_history=[],
            completed_today=[],
            paused=False,
            priority_queue=[],
            startup_status="complete",
            startup_message="",
            cached_queue_issues=[],
            pending_reviews=[],
            dependency_problems={},
        )
        self.config = self._create_mock_config()
        self._shutdown_requested = False
        self._event_hub = EventHub()

    @property
    def event_hub(self) -> EventHub:
        return self._event_hub

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @shutdown_requested.setter
    def shutdown_requested(self, value: bool) -> None:
        self._shutdown_requested = value

    def _create_mock_config(self) -> Config:
        config = Config()
        config.repo = "test/repo"
        config.max_concurrent_sessions = 3
        config.queue_refresh_seconds = 600
        config.ui_mode = "web"
        config.web_port = 8080
        config.config_path = Path("/tmp/config.yaml")
        config.repo_root = Path("/tmp/repo")
        config.worktree_base = Path("/tmp")
        config.filtering.label = None
        config.filter_milestone = None
        config.agents = {
            "agent:web": AgentConfig(
                prompt_path=Path("/tmp/prompt.txt"),
                model="sonnet",
                timeout_minutes=45,
            )
        }
        return config

    def pause(self) -> None:
        self.state.pause_state = operator_paused_state()

    def resume(self) -> None:
        self.state.pause_state = PauseState.running()

    def request_shutdown(self, force: bool = False) -> None:
        _ = force
        self._shutdown_requested = True

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        _ = inflight_stable_ids

    def get_failure_diagnosis(self, issue_number: int) -> dict[str, object]:
        return {
            "issue_number": issue_number,
            "ai_system": "unknown",
            "permission_mode": "default",
            "worktree_path": None,
            "log_path": None,
            "log_exists": False,
            "log_context": None,
            "history_status": None,
            "history_reason": None,
            "warnings": [],
            "suggestions": [],
        }
