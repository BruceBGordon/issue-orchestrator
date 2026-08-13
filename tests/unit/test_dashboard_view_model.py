"""Tests for dashboard view model generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.control.awaiting_merge_post_publish_policy import (
    POST_PUBLISH_VALIDATION_SOURCE,
)
from issue_orchestrator.domain.models import (
    AgentConfig,
    DependencyProblem,
    Issue,
    OrchestratorState,
    PendingReview,
    PendingRework,
    PendingValidationRetry,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import (
    NO_PROVIDER_CIRCUIT_STATUS,
    ProviderCircuitStatusReader,
)
from issue_orchestrator.view_models.dashboard import (
    _attach_running_timeline_snapshots,
    _normalize_status_reason,
    build_dashboard_view_model,
)
from issue_orchestrator.view_models.dashboard_flow import (
    apply_lane_precedence,
    build_awaiting_merge_items,
    exclude_flow_overlaps,
)
from issue_orchestrator.contracts.public import DashboardViewModelContract
from tests.unit.session_run_helpers import make_session_run_assets


@dataclass
class _OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


@dataclass(kw_only=True)
class _RouteOrchestratorStub(_OrchestratorStub):
    """Stub for tests that go through the HTTP route rather than the builder.

    ``provider_circuit`` is required with no default, mirroring the real
    orchestrator facade: the route resolves the circuit reader from it, and a
    stub that forgot to supply one must fail rather than read as a healthy
    provider fleet (issue #5980 F2/A1).
    """

    provider_circuit: ProviderCircuitStatusReader


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


def test_view_model_active_session_and_dashboard_data():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=12, title="Fix bug", labels=["agent:web"])
    session_key = SessionKey(issue=FakeIssueKey("12"), task=TaskKind.REVIEW)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="review-12",
        worktree_path=Path("/tmp/worktree-12"),
        branch_name="feature/12",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree-12"),
            session_name="review-12",
        ),
        started_at=datetime.now() - timedelta(minutes=5),
    )

    state = OrchestratorState(active_sessions=[session], startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.active_count == 1
    assert view_model.issues == view_model.active_items
    assert view_model.active_items[0]["status"] == "active"
    assert view_model.active_items[0]["flow_stage"] == "review"
    assert view_model.active_items[0]["card_id"] == "review-12"
    assert view_model.active_items[0]["action_hint"] == "Click to view agent UI log"
    assert view_model.flow_columns
    assert view_model.flow_columns[1]["id"] == "running"
    assert view_model.flow_columns[1]["count"] == 1
    assert view_model.flow_columns[1]["expandable"] is True
    assert view_model.flow_columns[1]["items"][0]["card_id"] == "review-12"

    dashboard_data = view_model.dashboard_data()
    assert dashboard_data["paused"] is False
    assert dashboard_data["queueRefreshSeconds"] == 600
    assert dashboard_data["agents"] == ["agent:web"]
    assert dashboard_data["configName"] == ""
    assert dashboard_data["configMode"] == "default"
    assert "scope" in dashboard_data
    assert dashboard_data["refresh"]["fetchLayerEnabled"] is True


def test_validation_configured_false_when_no_validation_command():
    # Issue #4109: the dashboard must be able to warn when agents push code
    # with no automated checks. A config with no validation command surfaces
    # validation_configured=False so the template renders the warning banner.
    config = _make_config()
    assert config.is_validation_enabled() is False

    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.validation_configured is False
    # The typed dashboard_data payload (DashboardDataContract) carries the flag
    # so the client/SSE contract exposes it too.
    assert view_model.dashboard_data()["validationConfigured"] is False


def test_validation_configured_true_when_validation_command_set():
    config = _make_config()
    config.validation.quick.cmd = "make validate"
    assert config.is_validation_enabled() is True

    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.validation_configured is True
    assert view_model.dashboard_data()["validationConfigured"] is True


def test_dashboard_data_exposes_e2e_failure_evidence_for_live_badge_updates():
    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        e2e_status_provider=lambda _: {
            "enabled": True,
            "running": False,
            "needs_attention": True,
            "last_run": {"id": 7, "status": "passed"},
            "failed_tests": [{"nodeid": "tests/e2e/test_smoke.py::test_checkout"}],
        },
    )

    dashboard_data = view_model.dashboard_data()
    assert dashboard_data["e2eNeedsAttention"] is True
    assert dashboard_data["e2eFailedTests"] == [
        {"nodeid": "tests/e2e/test_smoke.py::test_checkout"}
    ]


def test_running_flow_card_uses_latest_timeline_snapshot():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=409, title="Running flow item", labels=["agent:web", "in-progress"])
    session = Session(
        key=SessionKey(issue=FakeIssueKey("409"), task=TaskKind.CODE),
        issue=issue,
        agent_config=agent_config,
        terminal_id="issue-409",
        worktree_path=Path("/tmp/worktree-409"),
        branch_name="feature/409",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree-409"),
            session_name="issue-409",
        ),
        started_at=datetime.now() - timedelta(minutes=7),
    )

    state = OrchestratorState(active_sessions=[session], startup_status="complete")
    timeline_reader = MagicMock()
    timeline_reader.read.return_value.to_dict.return_value = {
        "events": [
            {
                "event": "session.started",
                "views": ["user", "ops", "debug"],
                "narrative": "Working on running timeline snapshot",
                "summary": "Restoring the running issue timeline snapshot on the dashboard",
            }
        ]
    }
    orchestrator = _OrchestratorStub(state=state, config=config)
    orchestrator.deps = type("_Deps", (), {"timeline_reader": timeline_reader})()

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.active_items[0]["summary"] == "Working on running timeline snapshot"
    running_column = next(column for column in view_model.flow_columns if column["id"] == "running")
    assert running_column["items"][0]["summary"] == "Working on running timeline snapshot"


def test_running_timeline_snapshot_reads_each_issue_once_when_cards_repeat():
    timeline_reader = MagicMock()
    timeline_reader.read.return_value.to_dict.return_value = {
        "events": [
            {
                "event": "session.started",
                "views": ["user"],
                "narrative": "Working on running timeline snapshot",
            }
        ]
    }
    orchestrator = _OrchestratorStub(state=OrchestratorState(startup_status="complete"), config=_make_config())
    orchestrator.deps = type("_Deps", (), {"timeline_reader": timeline_reader})()
    active_items = [
        {"issue_number": 409, "summary": ""},
        {"issue_number": 409, "summary": ""},
    ]

    _attach_running_timeline_snapshots(orchestrator, active_items)

    assert active_items[0]["summary"] == "Working on running timeline snapshot"
    assert active_items[1]["summary"] == "Working on running timeline snapshot"
    timeline_reader.read.assert_called_once_with(409, limit=40)


def test_active_item_prefers_canonical_issue_title_over_rework_title():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=4057, title="Rework #4124", labels=["agent:web", "in-progress"])
    session_key = SessionKey(issue=FakeIssueKey("4057"), task=TaskKind.REWORK)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="rework-4057",
        worktree_path=Path("/tmp/worktree-4057"),
        branch_name="feature/4057",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree-4057"),
            session_name="rework-4057",
        ),
        started_at=datetime.now() - timedelta(minutes=2),
    )

    state = OrchestratorState(
        active_sessions=[session],
        startup_status="complete",
        cached_queue_issues=[
            Issue(number=4057, title="[M9-009] UI: Surface provider circuit breaker status", labels=["agent:web"]),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    active = view_model.active_items[0]
    assert active["title"] == "UI: Surface provider circuit breaker status"
    # Synthetic session titles like "Rework #4124" have no external_id prefix —
    # the label must derive from the canonical issue title, not the raw session title.
    assert active["issue_key"] == "M9-009"
    assert active["issue_label"] == "M9-009 · #4057"


def test_view_model_queue_and_blocked_items():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    queued_issue = Issue(
        number=1,
        title="Queued",
        labels=["agent:web"],
        body="Depends-on: #5",
    )
    blocked_issue = Issue(
        number=2,
        title="Blocked",
        labels=["agent:web", "blocked"],
    )

    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[queued_issue, blocked_issue],
        pending_reviews=[
            PendingReview(
                issue_key=FakeIssueKey("1"),
                pr_number=101,
                pr_url="https://github.com/test/repo/pull/101",
                branch_name="feature/1",
                _issue_number=1,
            )
        ],
        dependency_problems={
            2: DependencyProblem(
                issue_number=2,
                issue_title="Blocked",
                blocked_by=[(5, "Dep", "open")],
                summary="Blocked - waiting on: #5",
            )
        },
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert len(view_model.queue_items) == 1
    assert len(view_model.blocked_items) == 1

    queue_item = view_model.queue_items[0]
    assert queue_item["issue_number"] == 1
    assert queue_item["flow_stage"] == "review"
    assert queue_item["has_dependencies"] is True
    assert "#5" in (queue_item["dependency_summary"] or "")

    blocked_item = view_model.blocked_items[0]
    assert blocked_item["issue_number"] == 2
    assert blocked_item["status"] == "blocked"
    assert "blocked" in (blocked_item["blocked_summary"] or "").lower()
    assert "waiting on" in (blocked_item["blocked_summary"] or "").lower()
    # Dependency-blocked items (issue #2) stay in blocked because they also have
    # the "blocked" label — only pure dependency blocks stay in queued
    assert view_model.blocked_count == 1


def test_tech_lead_observation_is_blocked_without_consuming_queue_position():
    config = _make_config()
    observation = Issue(
        number=39,
        title="Pattern case file",
        labels=["agent:tech-lead", "tech-lead-observation"],
    )
    runnable = Issue(number=45, title="Runnable", labels=["agent:backend"])
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[observation, runnable],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
    )

    assert [item["issue_number"] for item in view_model.queue_items] == [45]
    assert view_model.queue_items[0]["queue_wait_reason"] == "Waiting: next scheduler tick"
    blocked_item = next(item for item in view_model.blocked_items if item["issue_number"] == 39)
    assert blocked_item["blocked_summary"] == "Pattern case file (tech_lead observation ledger)"


def test_large_queue_counts_use_full_queue_not_preview_page():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    issues = [
        Issue(number=i, title=f"Queue Issue {i}", labels=["agent:web"])
        for i in range(1, 26)
    ]
    state = OrchestratorState(startup_status="complete", cached_queue_issues=issues)
    orchestrator = _OrchestratorStub(state=state, config=config)

    first_page = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )
    second_page = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=2,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in first_page.flow_columns if col["id"] == "queued")
    second_page_queued_col = next(col for col in second_page.flow_columns if col["id"] == "queued")

    assert first_page.queue_total == 25
    assert first_page.queue_total_pages == 2
    assert first_page.queue_count == 25
    assert first_page.scope_summary["repo_open_total"] == 25
    assert first_page.scope_summary["in_scope_total"] == 25
    assert queued_col["count"] == 25
    assert len(queued_col["items"]) == 12
    assert [item["issue_number"] for item in queued_col["items"]] == list(range(1, 13))
    assert [item["issue_number"] for item in first_page.queue_items] == list(range(1, 26))

    assert second_page.queue_count == 25
    assert second_page.scope_summary["in_scope_total"] == 25
    assert second_page_queued_col["count"] == 25
    assert [item["issue_number"] for item in second_page_queued_col["items"]] == list(range(21, 26))


def test_queue_preview_pages_follow_cached_queue_order_before_sorting():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    issues = [
        Issue(number=i, title=f"Queue Issue {i}", labels=["agent:web"])
        for i in range(25, 0, -1)
    ]
    state = OrchestratorState(startup_status="complete", cached_queue_issues=issues)
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=2,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    assert queued_col["count"] == 25
    assert [item["issue_number"] for item in queued_col["items"]] == [1, 2, 3, 4, 5]


def test_view_model_includes_refresh_freshness_metadata():
    config = _make_config()
    config.flow_refresh_stale_seconds = 60
    issue = Issue(number=21, title="Stale card", labels=["agent:web"])
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
    )
    state.queue_last_refresh_at = datetime.now().timestamp() - 300
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    assert queued_col["items"]
    card = queued_col["items"][0]
    assert card["issue_number"] == 21
    assert card["is_stale"] is True
    assert "ago" in card["last_refreshed_label"]
    refresh_meta = view_model.dashboard_data()["refresh"]
    assert refresh_meta["flowLazyEnabled"] is True
    assert refresh_meta["networkSyncSeconds"] == 60
    assert refresh_meta["flowStaleSeconds"] == 60
    assert refresh_meta["freshnessMode"] == "balanced"
    assert refresh_meta["apiBudget"] == "medium"
    assert refresh_meta["attentionPriority"] == "strict"

    gh_usage = view_model.dashboard_data()["githubUsage"]
    assert "total_calls" in gh_usage
    assert "calls_per_minute" in gh_usage


_FROZEN_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)


def _freeze_dashboard_clock(monkeypatch) -> None:
    """Pin ``datetime.now`` inside the dashboard view model.

    The view model computes its own ``now_ts`` at call time, so without a
    fixed reference a stop-the-world pause between the test's setup clock
    and the view-model's clock can push a "healthy" case past the stall
    threshold. Pinning both to the same instant removes the flake.
    """
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 — signature compat
            return _FROZEN_NOW

    monkeypatch.setattr(
        "issue_orchestrator.view_models.dashboard.datetime", _FrozenDatetime
    )


def test_stale_reason_blames_orchestrator_stall_when_tick_is_overdue(monkeypatch):
    """When the main loop hasn't completed a tick in >60s, say so (not the generic)."""
    _freeze_dashboard_clock(monkeypatch)
    config = _make_config()
    config.flow_refresh_stale_seconds = 60
    issue = Issue(number=42, title="Blocked card", labels=["agent:web"])
    now = _FROZEN_NOW.timestamp()
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
    )
    state.queue_last_refresh_at = now - 300
    # Loop started but never completed a tick → phase active_sessions has been
    # running for 3 minutes. This is the exact shape tixmeup hit during the
    # synchronous review exchange.
    state.last_tick_started_at = now - 180
    state.last_tick_completed_at = now - 200
    state.current_tick_phase = "active_sessions"
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    card = queued_col["items"][0]
    assert card["is_stale"] is True
    assert card["stale_reason"].startswith("Orchestrator tick stalled")
    assert "active_sessions" in card["stale_reason"]
    # Classic "Older than 15m threshold" must NOT appear — we have a more
    # informative story to tell.
    assert "stale threshold" not in card["stale_reason"]


def test_stale_reason_uses_threshold_text_when_tick_is_healthy(monkeypatch):
    """If the loop ticked recently, fall back to the legacy threshold message."""
    _freeze_dashboard_clock(monkeypatch)
    config = _make_config()
    config.flow_refresh_stale_seconds = 60
    issue = Issue(number=43, title="Stale but healthy orchestrator", labels=["agent:web"])
    now = _FROZEN_NOW.timestamp()
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
    )
    state.queue_last_refresh_at = now - 300  # stale by the GH-refresh clock
    state.last_tick_started_at = now - 2     # just ticked
    state.last_tick_completed_at = now - 2
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    card = queued_col["items"][0]
    assert card["is_stale"] is True
    assert "stale threshold" in card["stale_reason"]
    assert "Orchestrator tick stalled" not in card["stale_reason"]


def test_stale_reason_respects_configured_stall_threshold(monkeypatch):
    """Operators can loosen the stall-banner trigger via config."""
    _freeze_dashboard_clock(monkeypatch)
    config = _make_config()
    config.flow_refresh_stale_seconds = 60
    config.tick_stall_threshold_seconds = 300  # tolerate 5-minute ticks
    issue = Issue(number=44, title="Busy but not stalled", labels=["agent:web"])
    now = _FROZEN_NOW.timestamp()
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
    )
    state.queue_last_refresh_at = now - 600
    state.last_tick_completed_at = now - 120  # 2 min since last tick
    state.current_tick_phase = "active_sessions"
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    card = queued_col["items"][0]
    # Configured threshold is 300s and tick is only 120s old — no stall banner.
    assert "Orchestrator tick stalled" not in card["stale_reason"]
    assert "stale threshold" in card["stale_reason"]


def test_pr_pending_issue_not_shown_in_queued_flow_column():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    pr_pending_issue = Issue(
        number=4072,
        title="PR pending merge",
        labels=["agent:web", "pr-pending"],
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[pr_pending_issue],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    assert queued_col["count"] == 0
    assert all(item["issue_number"] != 4072 for item in queued_col["items"])
    assert any(item["issue_number"] == 4072 for item in view_model.awaiting_merge_items)
    assert view_model.scope_summary["in_scope_total"] == 1


def test_pr_pending_issue_queued_for_rework_leaves_merge_lane_with_reason():
    # Regression (#6588): when a post-publish merge conflict queues a PR for
    # rework, the source issue can still carry a stale pr-pending label for a
    # tick. It must surface as "Queued for rework" (with PR number, cycle, and
    # reason) in the queued lane — not linger in Awaiting Merge until launch.
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(
        number=454,
        title="Broken merge",
        labels=["agent:web", "pr-pending"],
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey("454"),
                agent_type="agent:web",
                rework_cycle=1,
                issue_number=454,
                pr_number=469,
                source=POST_PUBLISH_VALIDATION_SOURCE,
                feedback=(
                    "Merge conflict against base branch (cycle handled by "
                    "post-publish gate, not the reviewer):\n\nPR #469 was "
                    "approved but is no longer mergeable."
                ),
            )
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    awaiting_numbers = {item["issue_number"] for item in view_model.awaiting_merge_items}
    assert 454 not in awaiting_numbers

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    card = next(item for item in queued_col["items"] if item["issue_number"] == 454)
    assert card["state_label"] == "queued"
    assert card["phase"] == "Rework"
    summary = card["summary"]
    assert "Queued for rework" in summary
    assert "PR #469" in summary
    assert "cycle 1" in summary
    assert "Merge conflict against base branch" in summary


def test_queued_rework_issue_with_completed_history_pr_stays_queued_not_awaiting_merge():
    # Regression (#6593 F3): a queued-for-rework issue can ALSO have a stale
    # completed history row carrying the PR url. That completed+PR row set
    # merge_pending on the history lane, so build_awaiting_merge_items pulled
    # the issue into Awaiting Merge and lane precedence dropped it from Queued —
    # defeating the queued-rework owner. The owner must apply to the history
    # source too, not just the queue item.
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(
        number=454,
        title="Broken merge",
        labels=["agent:web", "pr-pending"],
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey("454"),
                agent_type="agent:web",
                rework_cycle=1,
                issue_number=454,
                pr_number=469,
                source=POST_PUBLISH_VALIDATION_SOURCE,
                feedback=(
                    "Merge conflict against base branch (cycle handled by "
                    "post-publish gate, not the reviewer):\n\nPR #469 was "
                    "approved but is no longer mergeable."
                ),
            )
        ],
        session_history=[
            SessionHistoryEntry(
                issue_number=454,
                title="Broken merge",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=12,
                pr_url="https://github.com/test/repo/pull/469",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    # The stale completed+PR history row must not surface the issue in Awaiting
    # Merge (list or column).
    awaiting_numbers = {item["issue_number"] for item in view_model.awaiting_merge_items}
    assert 454 not in awaiting_numbers
    awaiting_column = next(
        col for col in view_model.flow_columns if col["id"] == "awaiting-merge"
    )
    assert all(item["issue_number"] != 454 for item in awaiting_column["items"])

    # It stays in Queued with the queued-rework summary.
    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    card = next(item for item in queued_col["items"] if item["issue_number"] == 454)
    assert card["state_label"] == "queued"
    assert card["phase"] == "Rework"
    summary = card["summary"]
    assert "Queued for rework" in summary
    assert "PR #469" in summary
    assert "cycle 1" in summary


def test_pr_closed_blocked_issue_is_blocked_not_awaiting_merge():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    drifted_issue = Issue(
        number=4072,
        title="PR pending merge",
        labels=["agent:web", "pr-pending", "blocked:pr-closed"],
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[drifted_issue],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    blocked_numbers = {item["issue_number"] for item in view_model.blocked_items}
    awaiting_numbers = {item["issue_number"] for item in view_model.awaiting_merge_items}
    assert 4072 in blocked_numbers
    assert 4072 not in awaiting_numbers
    blocked_item = next(item for item in view_model.blocked_items if item["issue_number"] == 4072)
    assert "blocked:pr-closed" in blocked_item["orchestrator_labels"]
    assert blocked_item["blocked_summary"] == "PR closed or missing"


def test_completed_history_with_pr_url_routes_to_awaiting_merge_not_completed():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Provider circuit breaker dashboard",
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=12,
                pr_url="https://github.com/test/repo/pull/4124",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert any(item["issue_number"] == 4057 for item in view_model.awaiting_merge_items)
    awaiting_column = next(col for col in view_model.flow_columns if col["id"] == "awaiting-merge")
    awaiting_card = next(item for item in awaiting_column["items"] if item["issue_number"] == 4057)
    assert awaiting_card["pr_url"] == "https://github.com/test/repo/pull/4124"
    assert awaiting_card["github_url"] == "https://github.com/test/repo/pull/4124"
    assert awaiting_card["github_label"] == "PR ↗"
    assert awaiting_card["github_title"] == "Open PR on GitHub"
    assert all(item["issue_number"] != 4057 for item in view_model.completed_items)
    assert view_model.scope_summary["in_scope_total"] == 1


def test_awaiting_merge_history_card_retains_stack_gate_payload():
    # Regression (#6597): a completed-with-PR history entry can become the
    # awaiting-merge card (it wins the dedupe over the queue item). It must still
    # carry the producer stack payload/signal — otherwise a stacked successor
    # loses its merge-gate / approval-freshness chip in the exact lane operators
    # watch to see why the slice is still gated.
    from issue_orchestrator.domain.dependencies import (
        Dependency,
        DependencyMode,
        DependencyState,
    )
    from issue_orchestrator.domain.dependency_gates import (
        DependencyGateSnapshot,
        build_gate_report,
    )

    config = _make_config()
    # Stacked successor whose predecessor has not merged → merge gate blocked.
    dep = Dependency(issue_number=5, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    report = build_gate_report(4057, [dep])
    state = OrchestratorState(
        startup_status="complete",
        dependency_gate_snapshot=DependencyGateSnapshot(reports={4057: report}),
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Stacked successor awaiting merge",
                agent_type="agent:backend",
                status="completed",
                runtime_minutes=12,
                pr_url="https://github.com/test/repo/pull/4124",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    # The raw awaiting-merge item carries the projected stack payload + signal.
    item = next(i for i in view_model.awaiting_merge_items if i["issue_number"] == 4057)
    assert item["stack_dependency"] is not None
    assert item["stack_dependency"]["has_stack_edges"] is True
    assert "merge" in item["stack_dependency"]["blocked_gates"]
    assert item["stack_signal"]  # non-empty

    # ...and the rendered awaiting-merge flow-column card keeps them too, so the
    # compact stack chip renders and the fingerprint reflects the gate state.
    awaiting_column = next(c for c in view_model.flow_columns if c["id"] == "awaiting-merge")
    card = next(i for i in awaiting_column["items"] if i["issue_number"] == 4057)
    assert card["stack_dependency"] is not None
    assert card["stack_dependency"]["has_stack_edges"] is True
    assert card["stack_signal"]


def _stack_report_for(issue_number: int):
    """A stacked-successor gate report whose merge gate is blocked (predecessor
    not merged) — enough to make an issue a stack participant with a chip."""
    from issue_orchestrator.domain.dependencies import (
        Dependency,
        DependencyMode,
        DependencyState,
    )
    from issue_orchestrator.domain.dependency_gates import build_gate_report

    dep = Dependency(issue_number=5, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    return build_gate_report(issue_number, [dep])


def _assert_card_has_stack_payload(item):
    assert item["stack_dependency"] is not None
    assert item["stack_dependency"]["has_stack_edges"] is True
    assert "merge" in item["stack_dependency"]["blocked_gates"]
    assert item["stack_signal"]  # non-empty


def test_label_blocked_card_retains_stack_gate_payload():
    # Regression (#6597): a label-blocked stack participant is surfaced by the
    # scope-blocked builder, which does not spread the stack fields itself. The
    # finalization owner must stamp them so the blocked-column card keeps its chip.
    from issue_orchestrator.domain.dependency_gates import DependencyGateSnapshot

    config = _make_config()
    blocked_issue = Issue(number=7, title="Blocked stacked slice",
                          labels=["agent:web", "blocked"], body="Stack-after: #5")
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[blocked_issue],
        dependency_gate_snapshot=DependencyGateSnapshot(reports={7: _stack_report_for(7)}),
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    item = next(i for i in view_model.blocked_items if i["issue_number"] == 7)
    _assert_card_has_stack_payload(item)
    blocked_column = next(c for c in view_model.flow_columns if c["id"] == "blocked")
    card = next(i for i in blocked_column["items"] if i["issue_number"] == 7)
    _assert_card_has_stack_payload(card)


def test_pending_validation_retry_card_retains_stack_gate_payload():
    # Regression (#6597): a validation-retry card is a pending non-queue source
    # that also did not spread the stack fields. The finalization owner covers it.
    from issue_orchestrator.domain.dependency_gates import DependencyGateSnapshot

    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        dependency_gate_snapshot=DependencyGateSnapshot(reports={359: _stack_report_for(359)}),
        pending_validation_retries=[
            PendingValidationRetry(
                issue_number=359,
                issue_title="Validation retry stacked slice",
                agent_label="agent:backend",
                worktree_path="/tmp/repo-359",
                branch_name="issue-359",
                original_prompt="original task",
                validation_error="Working tree is dirty",
                validation_error_file="/tmp/repo-359/validation-errors.txt",
                retry_count=1,
                source_task=TaskKind.CODE,
                validation_cmd="./scripts/validate.sh",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    item = next(i for i in view_model.blocked_items if i["issue_number"] == 359)
    assert item["status"] == "validation_retry"
    _assert_card_has_stack_payload(item)
    blocked_column = next(c for c in view_model.flow_columns if c["id"] == "blocked")
    card = next(i for i in blocked_column["items"] if i["issue_number"] == 359)
    _assert_card_has_stack_payload(card)


def test_merged_history_with_pr_url_routes_to_completed_not_awaiting_merge():
    config = _make_config()
    pr_url = "https://github.com/test/repo/pull/4124"
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Provider circuit breaker dashboard",
                agent_type="agent:backend",
                status="merged",
                runtime_minutes=12,
                pr_url=pr_url,
                status_reason="PR merged; awaiting merge reconciled",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    awaiting_column = next(
        col for col in view_model.flow_columns if col["id"] == "awaiting-merge"
    )
    completed_column = next(
        col for col in view_model.flow_columns if col["id"] == "completed"
    )
    assert all(item["issue_number"] != 4057 for item in view_model.awaiting_merge_items)
    assert all(item["issue_number"] != 4057 for item in awaiting_column["items"])
    assert any(item["issue_number"] == 4057 for item in view_model.completed_items)
    assert any(item["issue_number"] == 4057 for item in completed_column["items"])
    completed_item = next(
        item for item in view_model.completed_items if item["issue_number"] == 4057
    )
    assert completed_item["status"] == "merged"
    assert completed_item["detail_label"] == "Merged"
    assert completed_item["merge_pending"] is False
    assert completed_item["pr_url"] == pr_url
    history_item = next(
        item for item in view_model.history_items if item["issue_number"] == 4057
    )
    assert history_item["status"] == "merged"
    assert history_item["detail_label"] == "Merged"
    assert history_item["merge_pending"] is False
    assert history_item["pr_url"] == pr_url
    assert history_item["show_stale_badge"] is False
    assert view_model.scope_summary["in_scope_total"] == 1


def test_history_completed_at_normalizes_naive_datetimes_to_utc_timestamp():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Provider circuit breaker dashboard",
                agent_type="agent:backend",
                status="merged",
                runtime_minutes=12,
                completed_at=datetime(2026, 5, 12, 10, 0, 0),
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    history_item = next(
        item for item in view_model.history_items if item["issue_number"] == 4057
    )
    assert history_item["time"] == "2026-05-12T10:00:00+00:00"
    assert history_item["time_is_timestamp"] is True
    assert history_item["runtime_label"] == "12 min"


def test_closed_history_with_pr_url_routes_to_completed_not_awaiting_merge():
    config = _make_config()
    pr_url = "https://github.com/test/repo/pull/4124"
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Provider circuit breaker dashboard",
                agent_type="agent:backend",
                status="closed",
                runtime_minutes=12,
                pr_url=pr_url,
                status_reason="Issue closed; awaiting merge reconciled",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    awaiting_column = next(
        col for col in view_model.flow_columns if col["id"] == "awaiting-merge"
    )
    completed_column = next(
        col for col in view_model.flow_columns if col["id"] == "completed"
    )
    assert all(item["issue_number"] != 4057 for item in view_model.awaiting_merge_items)
    assert all(item["issue_number"] != 4057 for item in awaiting_column["items"])
    assert any(item["issue_number"] == 4057 for item in view_model.completed_items)
    assert any(item["issue_number"] == 4057 for item in completed_column["items"])
    completed_item = next(
        item for item in view_model.completed_items if item["issue_number"] == 4057
    )
    assert completed_item["status"] == "closed"
    assert completed_item["detail_label"] == "Closed"
    assert completed_item["merge_pending"] is False
    assert completed_item["pr_url"] == pr_url
    history_item = next(
        item for item in view_model.history_items if item["issue_number"] == 4057
    )
    assert history_item["status"] == "closed"
    assert history_item["detail_label"] == "Closed"
    assert history_item["merge_pending"] is False
    assert history_item["pr_url"] == pr_url
    assert history_item["show_stale_badge"] is False
    assert view_model.scope_summary["in_scope_total"] == 1


def test_validation_failed_history_routes_to_blocked_lane():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4058,
                title="Validation gate failure",
                agent_type="agent:backend",
                status="validation_failed",
                runtime_minutes=12,
                status_reason="Validation failed after session completion",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    blocked_item = next(item for item in view_model.blocked_items if item["issue_number"] == 4058)
    assert blocked_item["status"] == "validation_failed"
    assert blocked_item["detail_label"] == "Validation Failed"
    assert blocked_item["flow_stage"] == "blocked"


def test_pending_validation_retry_routes_to_blocked_lane_and_suppresses_queue_duplicate():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[
            Issue(
                number=359,
                title="Validation retry item",
                labels=["agent:backend"],
            ),
        ],
        pending_validation_retries=[
            PendingValidationRetry(
                issue_number=359,
                issue_title="Validation retry item",
                agent_label="agent:backend",
                worktree_path="/tmp/repo-359",
                branch_name="issue-359",
                original_prompt="original task",
                validation_error="Working tree is dirty",
                validation_error_file="/tmp/repo-359/validation-errors.txt",
                retry_count=1,
                source_task=TaskKind.CODE,
                validation_cmd="./scripts/validate.sh",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    blocked_item = next(item for item in view_model.blocked_items if item["issue_number"] == 359)
    assert blocked_item["status"] == "validation_retry"
    assert blocked_item["detail_label"] == "Validation Retry Pending"
    assert blocked_item["flow_stage"] == "blocked"
    assert "Working tree is dirty" in blocked_item["blocked_summary"]
    assert all(item["issue_number"] != 359 for item in view_model.queue_items)

    blocked_column = next(column for column in view_model.flow_columns if column["id"] == "blocked")
    assert blocked_column["count"] == 1
    assert blocked_column["items"][0]["issue_number"] == 359


def test_pending_validation_retry_takes_precedence_over_validation_failed_history():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=359,
                title="Validation retry item",
                agent_type="agent:backend",
                status="validation_failed",
                runtime_minutes=12,
                status_reason="Earlier validation failure",
            ),
        ],
        pending_validation_retries=[
            PendingValidationRetry(
                issue_number=359,
                issue_title="Validation retry item",
                agent_label="agent:backend",
                worktree_path="/tmp/repo-359",
                branch_name="issue-359",
                original_prompt="original task",
                validation_error="Working tree is dirty",
                validation_error_file="/tmp/repo-359/validation-errors.txt",
                retry_count=1,
                source_task=TaskKind.CODE,
                validation_cmd="./scripts/validate.sh",
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    matching = [item for item in view_model.blocked_items if item["issue_number"] == 359]
    assert len(matching) == 1
    assert matching[0]["status"] == "validation_retry"
    assert "Working tree is dirty" in matching[0]["blocked_summary"]


def test_build_awaiting_merge_items_dedupes_union_preferring_pr_link():
    queue_item = {
        "issue_number": 280,
        "title": "Queue card",
        "status": "queue",
        "url": "https://github.com/test/repo/issues/280",
        "issue_url": "https://github.com/test/repo/issues/280",
        "pr_url": "",
        "merge_pending": True,
    }
    history_item = {
        "issue_number": 280,
        "title": "History card",
        "status": "completed",
        "url": "https://github.com/test/repo/pull/327",
        "issue_url": "https://github.com/test/repo/issues/280",
        "pr_url": "https://github.com/test/repo/pull/327",
        "merge_pending": True,
    }

    result = build_awaiting_merge_items(
        queue_items=[queue_item],
        blocked_items=[],
        history_items=[history_item],
    )

    assert result == [history_item]


def test_awaiting_merge_dedupes_queue_and_history_preferring_pr_link():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    pr_url = "https://github.com/test/repo/pull/327"
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[
            Issue(
                number=280,
                title="Click inference",
                labels=["agent:web", "pr-pending"],
            ),
        ],
        session_history=[
            SessionHistoryEntry(
                issue_number=280,
                title="Click inference",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=12,
                pr_url=pr_url,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="awaiting-merge",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert [item["issue_number"] for item in view_model.awaiting_merge_items] == [280]
    assert view_model.awaiting_merge_items[0]["pr_url"] == pr_url
    assert view_model.awaiting_merge_count == 1
    assert view_model.queue_count == 0
    assert view_model.completed_count == 0
    assert view_model.scope_summary["in_scope_total"] == 1

    awaiting_column = next(
        col for col in view_model.flow_columns if col["id"] == "awaiting-merge"
    )
    assert awaiting_column["count"] == 1
    assert len(awaiting_column["items"]) == 1
    awaiting_card = awaiting_column["items"][0]
    assert awaiting_card["issue_number"] == 280
    assert awaiting_card["github_url"] == pr_url
    assert awaiting_card["github_label"] == "PR ↗"


def test_completed_history_without_pr_url_does_not_enter_completed_lane():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    open_issue = Issue(
        number=4057,
        title="Provider circuit breaker dashboard",
        labels=["agent:web"],
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[open_issue],
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Provider circuit breaker dashboard",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=12,
                pr_url=None,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert all(item["issue_number"] != 4057 for item in view_model.completed_items)
    assert any(item["issue_number"] == 4057 for item in view_model.queue_items)


def test_queue_item_shows_textual_wait_reason():
    config = _make_config()
    issue = Issue(number=4057, title="Queued", labels=["agent:web"])
    state = OrchestratorState(startup_status="complete", cached_queue_issues=[issue])
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queue_item = view_model.queue_items[0]
    assert queue_item["flow_stage"] == "queued"
    assert queue_item["queue_wait_reason"].startswith("Waiting:")

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    card = queued_col["items"][0]
    assert card["summary"].startswith("Waiting:")
    assert card["queue_wait_reason"].startswith("Waiting:")


def test_queue_wait_reason_counts_only_runnable_items_ahead():
    config = _make_config()
    issues = [
        Issue(number=1, title="Dependency blocked", labels=["agent:web"]),
        Issue(number=2, title="Runnable ahead", labels=["agent:web"]),
        Issue(number=3, title="Also dependency blocked", labels=["agent:web"]),
        Issue(number=4, title="Runnable target", labels=["agent:web"]),
    ]
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=issues,
        dependency_problems={
            1: DependencyProblem(
                issue_number=1,
                issue_title="Dependency blocked",
                blocked_by=[(10, "Dep", "open")],
                summary="Blocked - waiting on: #10",
            ),
            3: DependencyProblem(
                issue_number=3,
                issue_title="Also dependency blocked",
                blocked_by=[(11, "Dep", "open")],
                summary="Blocked - waiting on: #11",
            ),
        },
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    target = next(item for item in view_model.queue_items if item["issue_number"] == 4)
    assert target["queue_wait_reason"] == "Waiting: 1 runnable queued ahead"

    dep_blocked = next(item for item in view_model.queue_items if item["issue_number"] == 1)
    assert dep_blocked["queue_wait_reason"] == "Waiting: Blocked - waiting on: #10"


def test_publish_failed_issue_routes_to_blocked_lane():
    config = _make_config()
    issue = Issue(number=4057, title="Publish failed", labels=["agent:web", "publish-failed"])
    state = OrchestratorState(startup_status="complete", cached_queue_issues=[issue])
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert all(item["issue_number"] != 4057 for item in view_model.queue_items)
    blocked_item = next(item for item in view_model.blocked_items if item["issue_number"] == 4057)
    assert blocked_item["flow_stage"] == "blocked"
    assert blocked_item["blocked_summary"]


def test_publish_failed_scope_issue_survives_blocked_lane_without_queue_entry():
    config = _make_config()
    issue = Issue(number=4057, title="Publish failed", labels=["agent:web", "publish-failed"])
    state = OrchestratorState(
        startup_status="complete",
        cached_scope_issues=[issue],
        cached_queue_issues=[],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert all(item["issue_number"] != 4057 for item in view_model.queue_items)
    blocked_item = next(item for item in view_model.blocked_items if item["issue_number"] == 4057)
    assert blocked_item["flow_stage"] == "blocked"
    assert blocked_item["blocked_summary"]


def test_blocked_column_reports_items_omitted_from_compact_preview():
    config = _make_config()
    issues = [
        Issue(
            number=issue_number,
            title=f"Blocked issue {issue_number}",
            labels=["agent:web", "blocked-failed"],
        )
        for issue_number in range(1, 14)
    ]
    state = OrchestratorState(
        startup_status="complete",
        cached_scope_issues=issues,
        cached_queue_issues=[],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    blocked_column = next(
        column for column in view_model.flow_columns if column["id"] == "blocked"
    )
    assert blocked_column["count"] == 13
    assert len(blocked_column["items"]) == 12
    assert blocked_column["hidden_count"] == 1


def test_review_stage_queue_item_does_not_get_queue_wait_reason():
    config = _make_config()
    issue = Issue(number=4057, title="Queued", labels=["agent:web"])
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
        pending_reviews=[
            PendingReview(
                issue_key=FakeIssueKey("4057"),
                pr_number=4124,
                pr_url="https://github.com/test/repo/pull/4124",
                branch_name="feature/4057",
                _issue_number=4057,
            )
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queue_item = view_model.queue_items[0]
    assert queue_item["flow_stage"] == "review"
    assert queue_item.get("queue_wait_reason") is None


def test_view_model_includes_refresh_staleness_meta():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.queue_refresh_seconds = 300
    queued_issue = Issue(number=22, title="Queued", labels=["agent:web"])

    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[queued_issue],
        issue_refresh_timestamps={22: datetime.now().timestamp() - 1200},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queue_item = view_model.queue_items[0]
    assert queue_item["refresh_age_label"]
    assert queue_item["refresh_age_seconds"] is not None
    assert queue_item["is_stale"] is True


def test_view_model_includes_refresh_staleness_meta():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.queue_refresh_seconds = 300
    queued_issue = Issue(number=22, title="Queued", labels=["agent:web"])

    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[queued_issue],
        issue_refresh_timestamps={22: datetime.now().timestamp() - 1200},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queue_item = view_model.queue_items[0]
    assert queue_item["refresh_age_label"]
    assert queue_item["refresh_age_seconds"] is not None
    assert queue_item["is_stale"] is True


def test_view_model_history_routing():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=10,
                title="Failed",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=12,
            ),
            SessionHistoryEntry(
                issue_number=11,
                title="Needs Human",
                agent_type="agent:web",
                status="needs_human",
                runtime_minutes=8,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="history",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    blocked_numbers = {item["issue_number"] for item in view_model.blocked_items}

    # Both failed (#10) and needs_human (#11) now go to blocked column
    assert 10 in blocked_numbers
    assert 11 in blocked_numbers


def test_history_items_expose_completed_at_as_timestamp_source():
    config = _make_config()
    completed_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=12,
                title="Merged",
                agent_type="agent:web",
                status="merged",
                runtime_minutes=12,
                completed_at=completed_at,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="completed",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    item = view_model.completed_items[0]
    assert item["time"] == completed_at.isoformat()
    assert item["time_is_timestamp"] is True
    assert item["runtime_label"] == "12 min"
    assert "@" not in item["time"]


def test_awaiting_merge_history_item_is_not_stale_when_startup_recovery_seeded_freshness():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.queue_refresh_seconds = 300
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Recovered awaiting merge",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=0,
                pr_url="https://github.com/owner/repo/pull/5337",
                status_reason="Recovered awaiting merge state on startup",
            )
        ],
        issue_refresh_timestamps={4057: datetime.now().timestamp()},
        issue_last_refreshed_at={4057: datetime.now().timestamp()},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    awaiting_column = next(col for col in view_model.flow_columns if col["id"] == "awaiting-merge")
    assert len(awaiting_column["items"]) == 1
    awaiting_card = awaiting_column["items"][0]
    assert awaiting_card["issue_number"] == 4057
    assert awaiting_card["is_stale"] is False
    assert awaiting_card["last_refreshed_age_seconds"] is not None


def test_completed_history_keeps_stale_fact_but_hides_stale_badge():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.flow_refresh_stale_seconds = 900
    now = datetime.now(timezone.utc).timestamp()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Merged terminal issue",
                agent_type="agent:web",
                status="closed",
                runtime_minutes=0,
                pr_url="https://github.com/owner/repo/pull/5337",
                status_reason="PR closed; awaiting merge reconciled",
            )
        ],
        issue_last_refreshed_at={4057: now - 3600},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    completed_card = next(
        item for item in view_model.completed_items if item["issue_number"] == 4057
    )
    assert completed_card["is_stale"] is True
    assert completed_card["stale_reason"] == "Older than 15m stale threshold"
    assert completed_card["show_stale_badge"] is False
    history_item = next(
        item for item in view_model.history_items if item["issue_number"] == 4057
    )
    assert history_item["is_stale"] is True
    assert history_item["show_stale_badge"] is False

    completed_column = next(col for col in view_model.flow_columns if col["id"] == "completed")
    column_card = next(
        item for item in completed_column["items"] if item["issue_number"] == 4057
    )
    assert column_card["is_stale"] is True
    assert column_card["show_stale_badge"] is False


def test_awaiting_merge_history_stale_fact_shows_stale_badge():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.flow_refresh_stale_seconds = 900
    now = datetime.now(timezone.utc).timestamp()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4058,
                title="Awaiting merge issue",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=0,
                pr_url="https://github.com/owner/repo/pull/5338",
                status_reason="PR created successfully",
            )
        ],
        issue_last_refreshed_at={4058: now - 3600},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    awaiting_card = next(
        item for item in view_model.awaiting_merge_items if item["issue_number"] == 4058
    )
    assert awaiting_card["is_stale"] is True
    assert awaiting_card["show_stale_badge"] is True
    history_item = next(
        item for item in view_model.history_items if item["issue_number"] == 4058
    )
    assert history_item["is_stale"] is True
    assert history_item["show_stale_badge"] is True

    awaiting_column = next(col for col in view_model.flow_columns if col["id"] == "awaiting-merge")
    column_card = next(
        item for item in awaiting_column["items"] if item["issue_number"] == 4058
    )
    assert column_card["is_stale"] is True
    assert column_card["show_stale_badge"] is True


def test_exclude_flow_overlaps_handles_string_issue_numbers():
    backlog_items = [{"issue_number": 4070, "title": "Backlog"}]
    queue_items = [{"issue_number": "4070", "title": "Queued"}]

    result = exclude_flow_overlaps(
        backlog_items=backlog_items,
        queue_items=queue_items,
        active_items=[],
        blocked_items=[],
        completed_items=[],
    )

    assert result == []


def test_lane_precedence_enforces_single_lane_membership():
    queue_items = [
        {"issue_number": 1, "title": "Queue 1", "is_stale": False},
        {"issue_number": 5, "title": "Queue 5", "is_stale": True},
    ]
    active_items = [
        {"issue_number": 2, "title": "Running 2a", "is_stale": False},
        # Multiple running cards for same issue are allowed
        {"issue_number": 2, "title": "Running 2b", "is_stale": True},
    ]
    blocked_items = [
        {"issue_number": 2, "title": "Blocked 2", "is_stale": True},
        {"issue_number": 3, "title": "Blocked 3", "is_stale": True},
    ]
    awaiting_merge_items = [
        {"issue_number": 3, "title": "Awaiting 3", "is_stale": True},
        {"issue_number": 4, "title": "Awaiting 4", "is_stale": True},
    ]
    completed_items = [
        {"issue_number": 1, "title": "Done 1", "is_stale": True},
        {"issue_number": 4, "title": "Done 4", "is_stale": True},
        {"issue_number": 6, "title": "Done 6", "is_stale": True},
    ]

    queue_out, blocked_out, awaiting_out, completed_out = apply_lane_precedence(
        queue_items=queue_items,
        active_items=active_items,
        blocked_items=blocked_items,
        awaiting_merge_items=awaiting_merge_items,
        completed_items=completed_items,
    )

    assert [i["issue_number"] for i in blocked_out] == [3]  # #2 suppressed by running
    assert [i["issue_number"] for i in awaiting_out] == [4]  # #3 suppressed by blocked
    assert [i["issue_number"] for i in queue_out] == [1, 5]  # unchanged here
    assert [i["issue_number"] for i in completed_out] == [6]  # #1/#4 suppressed by queue/awaiting
    assert [i["show_stale_badge"] for i in queue_out] == [False, True]
    assert [i["show_stale_badge"] for i in blocked_out] == [True]
    assert [i["show_stale_badge"] for i in awaiting_out] == [True]
    assert [i["show_stale_badge"] for i in completed_out] == [False]


def test_normalize_status_reason_drops_sync_noise() -> None:
    assert _normalize_status_reason("Synced 10s ago") is None
    assert _normalize_status_reason(" synced 5m ago ") is None
    assert _normalize_status_reason("blocked by dependency #100") == "blocked by dependency #100"


def test_view_model_history_dedupes_latest_per_issue():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=10,
                title="Failed First",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=12,
            ),
            SessionHistoryEntry(
                issue_number=10,
                title="Blocked Latest",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=3,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="history",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    combined = view_model.history_items + view_model.blocked_items
    assert len([item for item in combined if item["issue_number"] == 10]) == 1
    assert any(item["status"] == "blocked" for item in combined if item["issue_number"] == 10)


def test_completed_excludes_issues_visible_in_running_lane():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    issue = Issue(number=12, title="Fix bug", labels=["agent:web"])
    active_session = Session(
        key=SessionKey(issue=FakeIssueKey("12"), task=TaskKind.CODE),
        issue=issue,
        agent_config=agent_config,
        terminal_id="issue-12",
        worktree_path=Path("/tmp/worktree-12"),
        branch_name="feature/12",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree-12"),
            session_name="issue-12",
        ),
        started_at=datetime.now() - timedelta(minutes=1),
    )
    state = OrchestratorState(
        startup_status="complete",
        active_sessions=[active_session],
        session_history=[
            SessionHistoryEntry(
                issue_number=12,
                title="Fix bug",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert any(item["issue_number"] == 12 for item in view_model.active_items)
    assert all(item["issue_number"] != 12 for item in view_model.completed_items)


def test_view_model_e2e_items_from_provider():
    config = _make_config()
    config.e2e.enabled = True
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    def e2e_status_provider(_):
        return {
            "enabled": True,
            "running": True,
            "needs_attention": True,
            "untriaged_count": 2,
            "last_run": {
                "id": 7,
                "relative_time": "1h ago",
                "started_at": "2026-05-12T10:00:00Z",
            },
            "failed_tests": [
                {"nodeid": "tests/test_a.py::test_x", "duration_seconds": 1.2},
            ],
        }

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="e2e",
        e2e_page=1,
        e2e_status_provider=e2e_status_provider,
    )

    assert view_model.e2e_count == 2
    assert len(view_model.e2e_items) == 2
    assert view_model.issues == view_model.e2e_items
    assert any(item.get("e2e_running") for item in view_model.e2e_items)
    assert any(item.get("status") == "needs_attention" for item in view_model.e2e_items)
    assert all(item["show_stale_badge"] is False for item in view_model.e2e_items)
    attention_item = next(item for item in view_model.e2e_items if item.get("status") == "needs_attention")
    assert attention_item["started_at"] == "2026-05-12T10:00:00Z"
    assert attention_item["time"] == "2026-05-12T10:00:00Z"
    assert attention_item["time_is_timestamp"] is True
    e2e_vm = view_model.e2e_status.get("view_model", {})
    assert e2e_vm.get("badge", {}).get("state") in {"failed", "running", "passed", "warning", "idle"}
    assert isinstance(e2e_vm.get("runs"), list)


def test_view_model_api_endpoint():
    from fastapi.testclient import TestClient
    from issue_orchestrator.entrypoints import web
    from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator

    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _RouteOrchestratorStub(
        state=state, config=config, provider_circuit=NO_PROVIDER_CIRCUIT_STATUS
    )

    original = get_orchestrator()
    set_orchestrator(orchestrator)
    try:
        client = TestClient(web.app)
        response = client.get("/api/view-model")
        assert response.status_code == 200
        data = response.json()
        assert data["dashboard_data"]["repo"] == "test/repo"
        assert data["dashboard_data"]["queueRefreshSeconds"] == 600
    finally:
        set_orchestrator(original)


def test_view_model_matches_public_contract():
    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    DashboardViewModelContract.model_validate(view_model.to_dict())


def test_e2e_recent_run_item_carries_note(tmp_path):
    """When an e2e run has a note, build_e2e_recent_run_items must include it."""
    from issue_orchestrator.infra.e2e_db import E2EDB
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_recent_run_items

    config = _make_config()
    # orchestrator_id is derived from repo_root.name
    orch_id = config.orchestrator_id

    db = E2EDB(tmp_path / "e2e.db")
    run_id = db.start_run(
        repo_root=str(tmp_path),
        orchestrator_id=orch_id,
        pytest_args=[],
    )
    db.finish_run(
        run_id,
        status="failed",
        exit_code=1,
        note="Fixture errors: test_foo (teardown): GH activity exceeded limit",
    )

    items = build_e2e_recent_run_items(db, config, {"enabled": True, "running": False})

    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["note"] == "Fixture errors: test_foo (teardown): GH activity exceeded limit"


def test_e2e_recent_run_item_omits_note_when_none(tmp_path):
    """When an e2e run has no note, the item must not have a note key."""
    from issue_orchestrator.infra.e2e_db import E2EDB
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_recent_run_items

    config = _make_config()
    orch_id = config.orchestrator_id

    db = E2EDB(tmp_path / "e2e.db")
    run_id = db.start_run(
        repo_root=str(tmp_path),
        orchestrator_id=orch_id,
        pytest_args=[],
    )
    db.finish_run(run_id, status="passed", exit_code=0)

    items = build_e2e_recent_run_items(db, config, {"enabled": True, "running": False})

    assert len(items) == 1
    assert "note" not in items[0]


def test_e2e_recent_run_item_exposes_timestamp_source_for_dashboard_formatting(tmp_path):
    """Recent E2E rows carry timestamp data, not preformatted relative text."""
    from issue_orchestrator.infra.e2e_db import E2EDB
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_recent_run_items

    config = _make_config()
    orch_id = config.orchestrator_id

    db = E2EDB(tmp_path / "e2e.db")
    run_id = db.start_run(
        repo_root=str(tmp_path),
        orchestrator_id=orch_id,
        pytest_args=[],
    )
    db.finish_run(run_id, status="passed", exit_code=0)

    items = build_e2e_recent_run_items(db, config, {"enabled": True, "running": False})

    assert len(items) == 1
    assert items[0]["started_at"]
    assert items[0]["time"] == items[0]["started_at"]
    assert items[0]["time_is_timestamp"] is True
    assert "relative_time" not in items[0]


def test_e2e_recent_run_item_exposes_typed_open_run_command(tmp_path):
    """Every recent-run item must carry the typed ``open_run_command``.

    PR #6329 contract: the dashboard chip serializes the view-model
    field via ``{{ run.open_run_command | tojson | forceescape }}``.
    If the view model stops emitting it, the chip's
    ``data-lifecycle-command`` attribute renders as ``null`` and the
    dispatcher silently no-ops on the click.  This test pins the
    emission at the model layer.
    """
    from issue_orchestrator.infra.e2e_db import E2EDB
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_recent_run_items

    config = _make_config()
    orch_id = config.orchestrator_id
    db = E2EDB(tmp_path / "e2e.db")
    run_id = db.start_run(repo_root=str(tmp_path), orchestrator_id=orch_id, pytest_args=[])
    db.finish_run(run_id, status="passed", exit_code=0)

    items = build_e2e_recent_run_items(db, config, {"enabled": True, "running": False})
    assert len(items) == 1
    item = items[0]
    assert item["e2e_run_id"] == run_id
    # The typed Command must be emitted as a dict matching the
    # ``OpenE2ERunCommand.model_dump()`` shape — same fields the
    # OpenAPI schema validates.
    assert item["open_run_command"] == {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": run_id,
        "expand_run_details": False,
    }


def test_e2e_recent_run_item_exposes_formatted_results_action(tmp_path):
    """Passed runs still need an explicit action into the formatted results modal."""
    from issue_orchestrator.infra.e2e_db import E2EDB
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_recent_run_items

    config = _make_config()
    orch_id = config.orchestrator_id

    db = E2EDB(tmp_path / "e2e.db")
    run_id = db.start_run(
        repo_root=str(tmp_path),
        orchestrator_id=orch_id,
        pytest_args=[],
    )
    db.finish_run(run_id, status="passed", exit_code=0)

    items = build_e2e_recent_run_items(db, config, {"enabled": True, "running": False})

    assert len(items) == 1
    assert items[0]["results_action"] == {
        "kind": "e2e_run_results",
        "run_id": run_id,
        "label": "View Results",
    }


def test_e2e_badge_state_maps_warning():
    """The E2E view model must map last_run status='warning' to badge_state='warning'."""
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_view_model

    vm = build_e2e_view_model(
        e2e_status={
            "running": False,
            "needs_attention": False,
            "last_run": {"id": 1, "status": "warning"},
        },
        e2e_items=[],
        e2e_total=1,
        e2e_page=1,
        e2e_total_pages=1,
        agents=[],
    )
    assert vm["badge"]["state"] == "warning"
    assert vm["badge"]["icon"] == "⚠"


def test_e2e_view_model_exposes_latest_formatted_results_action():
    """The E2E summary owns the latest-run View Results action contract."""
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_view_model

    vm = build_e2e_view_model(
        e2e_status={
            "running": False,
            "needs_attention": False,
            "last_run": {"id": 42, "status": "passed", "relative_time": "3m ago"},
        },
        e2e_items=[],
        e2e_total=1,
        e2e_page=1,
        e2e_total_pages=1,
        agents=[],
    )

    assert vm["summary"]["results_action"] == {
        "kind": "e2e_run_results",
        "run_id": 42,
        "label": "View Results",
    }


def test_e2e_badge_state_failed_when_passed_run_has_failed_test_evidence():
    """Parsed failing tests must win over a superficially passed run status."""
    from issue_orchestrator.view_models.dashboard_e2e import build_e2e_view_model

    vm = build_e2e_view_model(
        e2e_status={
            "running": False,
            "needs_attention": False,
            "last_run": {"id": 7, "status": "passed"},
            "failed_tests": [{"nodeid": "tests/e2e/test_smoke.py::test_checkout"}],
        },
        e2e_items=[],
        e2e_total=1,
        e2e_page=1,
        e2e_total_pages=1,
        agents=[],
    )

    assert vm["badge"]["state"] == "failed"
    assert vm["badge"]["icon"] == "✗"


def test_queue_card_embeds_producer_stack_gate_view():
    # A queued stack successor's card must carry the producer-provided stack
    # gate view (mode + per-gate status), sourced from the state snapshot rather
    # than recomputed in the view model.
    from issue_orchestrator.domain.dependencies import (
        Dependency,
        DependencyMode,
        DependencyState,
        DependencyTarget,
    )
    from issue_orchestrator.domain.dependency_gates import (
        DependencyGateSnapshot,
        PredecessorFacts,
        SuccessorEdge,
        build_gate_report,
    )

    config = _make_config()
    config.agents = {"agent:web": _make_agent_config()}

    successor = Issue(number=1, title="Successor", labels=["agent:web"],
                      body="Stack-after: #5")
    dep = Dependency(issue_number=5, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    facts = {DependencyTarget(issue_number=5): PredecessorFacts(
        branch_usable=True, validation_passed=True, agent_reviewed=True,
        branch_name="feat/base",
    )}
    report = build_gate_report(1, [dep], facts)
    snapshot = DependencyGateSnapshot(
        reports={1: report},
        successors={5: (SuccessorEdge(issue_number=1, ref="#1", mode=DependencyMode.STACK),)},
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[successor],
        dependency_gate_snapshot=snapshot,
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    card = next(c for c in view_model.queue_items if c["issue_number"] == 1)
    stack = card["stack_dependency"]
    assert stack is not None
    assert stack["mode"] == "stack"
    assert stack["has_stack_edges"] is True
    # Work is ready but merge stays ordered behind the predecessor.
    gates = {g["gate"]: g["open"] for g in stack["gates"]}
    assert gates["work"] is True
    assert gates["merge"] is False
    assert "merge" in stack["blocked_gates"]
    assert stack["stack_base_branch"] == "feat/base"
