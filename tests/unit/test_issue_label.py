"""Tests for the issue display label formatter shared by domain + view models."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from issue_orchestrator.domain.issue_key import (
    ISSUE_LABEL_SEPARATOR,
    FakeIssueKey,
    format_issue_label,
    issue_label_parts,
)
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.dashboard_flow import compact_card
from tests.unit.session_run_helpers import make_session_run_assets


def test_separator_is_middle_dot_with_spaces():
    """Pinning the separator catches accidental whitespace/character drift."""
    assert ISSUE_LABEL_SEPARATOR == " · "


def test_format_with_both_key_and_number():
    assert format_issue_label(274, "M9-009") == "M9-009 · #274"


def test_format_with_only_number():
    assert format_issue_label(274, None) == "#274"
    assert format_issue_label(274, "") == "#274"


def test_format_with_only_key():
    assert format_issue_label(None, "M9-009") == "M9-009"


def test_format_with_neither_returns_empty():
    assert format_issue_label(None, None) == ""


def test_label_parts_orders_key_first():
    """The order function controls display order project-wide."""
    assert issue_label_parts("M9-009", 274) == ("M9-009", "#274")


def _make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 600
    config.terminal_adapter = "subprocess"
    config.e2e.enabled = False
    return config


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )


def test_compact_card_derives_label_from_issue_key_and_number():
    card = compact_card({"issue_number": 274, "issue_key": "M9-009", "title": "x", "show_stale_badge": False})
    assert card["issue_label"] == "M9-009 · #274"
    assert card["issue_key"] == "M9-009"


def test_compact_card_falls_back_to_number_only_when_no_key():
    card = compact_card({"issue_number": 274, "title": "x", "show_stale_badge": False})
    assert card["issue_label"] == "#274"
    assert card["issue_key"] is None


def test_compact_card_uses_precomputed_issue_label_when_provided():
    card = compact_card({"issue_number": 1, "issue_label": "custom · #1", "show_stale_badge": False})
    assert card["issue_label"] == "custom · #1"


def test_dashboard_view_model_active_card_carries_issue_label():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=274, title="[M9-009] Add label everywhere", labels=["agent:web"])
    session = Session(
        key=SessionKey(issue=FakeIssueKey("274"), task=TaskKind.CODE),
        issue=issue,
        agent_config=agent_config,
        terminal_id="issue-274",
        worktree_path=Path("/tmp/wt"),
        branch_name="feature/274",
        run_assets=make_session_run_assets(Path("/tmp/wt"), session_name="issue-274"),
        started_at=datetime.now() - timedelta(minutes=1),
    )
    state = OrchestratorState(active_sessions=[session], startup_status="complete")

    class _Stub:
        def __init__(self) -> None:
            self.state = state
            self.config = config
            self.shutdown_requested = False

    view_model = build_dashboard_view_model(
        _Stub(),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    active = view_model.active_items[0]
    assert active["issue_key"] == "M9-009"
    assert active["issue_label"] == "M9-009 · #274"
    running_column = next(c for c in view_model.flow_columns if c["id"] == "running")
    assert running_column["items"][0]["issue_label"] == "M9-009 · #274"


def test_dashboard_view_model_history_card_carries_issue_label():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    entry = SessionHistoryEntry(
        issue_number=274,
        title="[M9-009] Add label everywhere",
        agent_type="agent:web",
        status="completed",
        runtime_minutes=24,
        pr_url="https://github.com/test/repo/pull/352",
    )
    state = OrchestratorState(
        startup_status="complete",
        session_history=[entry],
    )

    class _Stub:
        def __init__(self) -> None:
            self.state = state
            self.config = config
            self.shutdown_requested = False

    view_model = build_dashboard_view_model(
        _Stub(),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="awaiting-merge",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    awaiting = view_model.awaiting_merge_items[0]
    assert awaiting["issue_key"] == "M9-009"
    assert awaiting["issue_label"] == "M9-009 · #274"
    awaiting_column = next(c for c in view_model.flow_columns if c["id"] == "awaiting-merge")
    assert awaiting_column["items"][0]["issue_label"] == "M9-009 · #274"
