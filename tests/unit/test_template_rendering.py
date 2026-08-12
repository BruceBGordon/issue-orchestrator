"""Template rendering tests using Jinja2 + BeautifulSoup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState, Session, SessionHistoryEntry
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_CSS_CHUNKS
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_JS_CHUNKS
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from tests.unit.session_run_helpers import make_session_run_assets


TEMPLATE_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates"
STATIC_JS_DIR = (
    Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "static" / "js"
)


def read_dashboard_js_bundle() -> str:
    return "\n".join(
        (STATIC_JS_DIR / "dashboard" / chunk).read_text(encoding="utf-8")
        for chunk in DASHBOARD_JS_CHUNKS
    )


@pytest.fixture
def jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


@dataclass
class OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


def make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 600
    config.terminal_adapter = "subprocess"
    config.filtering.milestones = ["M7"]
    config.filtering.label = "agent-ready"
    config.e2e.enabled = False
    return config


def make_agent_config() -> AgentConfig:
    return AgentConfig(prompt_path=Path("/tmp/prompt.txt"), model="sonnet", timeout_minutes=45)


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE) -> Session:
    agent_config = make_agent_config()
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=task),
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(f"/tmp/worktree-{issue.number}"),
        branch_name=f"feature/{issue.number}",
        run_assets=make_session_run_assets(
            Path(f"/tmp/worktree-{issue.number}"),
            session_name=f"issue-{issue.number}",
        ),
        started_at=datetime.now() - timedelta(minutes=6),
    )


def render_dashboard(jinja_env: Environment, view_model) -> BeautifulSoup:
    template = jinja_env.get_template("dashboard.html")
    html = template.render(**view_model.template_context())
    return BeautifulSoup(html, "html.parser")


def e2e_disabled(_config) -> dict:
    return {"enabled": False, "running": False}


def test_flow_dashboard_renders_columns_and_scope(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    issue = Issue(number=101, title="Ship board", labels=["agent:web"])
    state = OrchestratorState(
        startup_status="complete",
        active_sessions=[make_session(issue)],
        cached_queue_issues=[Issue(number=102, title="Queued", labels=["agent:web"])],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    active_tab = soup.select_one("#tab-dashboard.active")
    assert active_tab is not None
    assert active_tab.get("data-tab") == "kanban"
    columns = soup.select(".kanban-column")
    assert len(columns) == 5
    column_ids = [col["data-column"] for col in columns]
    assert column_ids == ["queued", "running", "blocked", "awaiting-merge", "completed"]
    assert "milestones=M7" in soup.select_one(".scope-summary").text


def test_first_paint_renders_stack_chip_for_stacked_card(jinja_env):
    # F1 (#6597): the server template must render the compact stack chip on the
    # first paint. The card fingerprint already includes stack_signal, so if the
    # first-paint DOM lacks the chip, the first client refresh takes the
    # fingerprint-matched branch and never repairs it — the chip would stay hidden.
    from issue_orchestrator.domain.dependencies import (
        Dependency,
        DependencyMode,
        DependencyState,
    )
    from issue_orchestrator.domain.dependency_gates import (
        DependencyGateSnapshot,
        build_gate_report,
    )

    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    issue = Issue(number=201, title="Stacked successor", labels=["agent:web"],
                  body="Stack-after: #5")
    dep = Dependency(issue_number=5, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
        dependency_gate_snapshot=DependencyGateSnapshot(
            reports={201: build_gate_report(201, [dep])}
        ),
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    card = soup.select_one('.issue-card[data-issue="201"]')
    assert card is not None
    assert card.get("data-card-fingerprint")  # fingerprint is stamped on first paint
    assert card.select_one(".stack-chip") is not None
    status = card.select_one(".stack-chip-status")
    assert status is not None
    assert status.text.strip()  # text status, not colour only


def test_dashboard_renders_manifest_js_chunks_in_order(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    script_sources = [
        script.get("src")
        for script in soup.find_all("script")
        if script.get("src")
    ]
    expected_chunks = [
        f"/static/js/dashboard/{chunk}"
        for chunk in DASHBOARD_JS_CHUNKS
    ]
    chunk_start = script_sources.index(expected_chunks[0])
    assert script_sources[chunk_start : chunk_start + len(expected_chunks)] == expected_chunks
    assert script_sources[chunk_start - 1] == "/static/vendor/xterm/addon-fit.js"
    assert script_sources[chunk_start + len(expected_chunks)] == "/static/js/dashboard.js"


def test_dashboard_renders_manifest_css_chunks_before_late_styles(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    stylesheet_hrefs = [
        link.get("href")
        for link in soup.find_all("link", rel="stylesheet")
        if link.get("href")
    ]
    expected_chunks = [
        f"/static/css/dashboard/{chunk}"
        for chunk in DASHBOARD_CSS_CHUNKS
    ]
    chunk_start = stylesheet_hrefs.index(expected_chunks[0])
    assert stylesheet_hrefs[chunk_start : chunk_start + len(expected_chunks)] == expected_chunks
    assert "/static/css/dashboard.css" not in stylesheet_hrefs
    assert stylesheet_hrefs[chunk_start + len(expected_chunks)] == "/static/css/ui_primitives.css"


def test_dashboard_js_compact_renderer_routes_running_cancel_to_menu():
    source = read_dashboard_js_bundle()
    assert "const hasTerminal = card.state_label === 'running' ? 'true' : 'false';" in source
    assert "data-has-terminal" in source
    assert "class=\"card-kill-btn\"" not in source
    assert "class=\"card-menu-btn\"" in source
    assert "openCompactCardActionsMenu(" in source


def test_dashboard_js_switch_tab_shows_loading_state():
    source = read_dashboard_js_bundle()
    assert "tab-nav-pending" in source
    assert "is-loading" in source
    assert "aria-busy" in source


def test_kanban_blocked_column_is_expandable(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    blocked = Issue(number=210, title="Blocked merge", labels=["agent:web", "blocked-needs-human"])
    state = OrchestratorState(startup_status="complete", cached_queue_issues=[blocked])
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    blocked_col = soup.select_one('[data-column="blocked"]')
    assert blocked_col is not None
    assert "expandable" in blocked_col.get("class", [])
    assert blocked_col.select_one(".column-expand-btn") is not None
    # Blocked column has tech_lead filter bar
    assert blocked_col.select_one(".column-filter-bar") is not None
    filter_btns = blocked_col.select(".filter-btn")
    assert len(filter_btns) == 3
    assert [btn.text.strip() for btn in filter_btns] == ["All", "New", "Viewed"]
    badge_texts = [badge.text.strip() for badge in blocked_col.select(".card-badges .badge")]
    assert "agent:web" in badge_texts


def test_truncated_blocked_column_renders_full_list_footer(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    blocked_issues = [
        Issue(
            number=issue_number,
            title=f"Blocked issue {issue_number}",
            labels=["agent:web", "blocked-failed"],
        )
        for issue_number in range(1, 14)
    ]
    state = OrchestratorState(
        startup_status="complete",
        cached_scope_issues=blocked_issues,
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)
    blocked_col = soup.select_one('[data-column="blocked"]')
    assert blocked_col is not None
    footer = blocked_col.select_one(".column-overflow-footer")
    assert footer is not None
    assert footer.get_text(" ", strip=True) == "… 1 more"
    assert footer.get("hidden") is None
    assert footer.get("aria-controls") == "column-blocked-expanded"
    assert footer.get("aria-expanded") == "false"
    assert footer.get("onclick") == "expandColumnFromOverflow(this)"

    expanded = blocked_col.select_one("#column-blocked-expanded")
    assert expanded is not None
    assert expanded.get("role") == "region"
    assert expanded.get("aria-label") == "Blocked full list"
    assert expanded.get("aria-labelledby") is None


def test_complete_compact_column_keeps_full_list_footer_hidden(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[
            Issue(number=1, title="Only item", labels=["agent:web"]),
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)
    queued_col = soup.select_one('[data-column="queued"]')
    assert queued_col is not None
    footer = queued_col.select_one(".column-overflow-footer")
    assert footer is not None
    assert footer.has_attr("hidden")


def test_kanban_running_column_is_expandable_and_routes_cancel_to_menu(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    running_issue = Issue(number=4057, title="Running issue", labels=["agent:web", "in-progress"])
    state = OrchestratorState(startup_status="complete", active_sessions=[make_session(running_issue)])
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    running_col = soup.select_one('[data-column="running"]')
    assert running_col is not None
    assert "expandable" in running_col.get("class", [])
    assert running_col.select_one(".column-expand-btn") is not None
    assert running_col.select_one(".card-kill-btn") is None
    menu_btn = running_col.select_one(".card-menu-btn")
    assert menu_btn is not None
    assert menu_btn.get("data-has-terminal") == "true"


def test_kanban_completed_column_session_scoped(jinja_env):
    config = make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=7,
                title="Done issue",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=9,
                pr_url="https://example.test/pr/7",
            )
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    awaiting_merge_col = soup.select_one('[data-column="awaiting-merge"]')
    assert awaiting_merge_col is not None
    assert "expandable" in awaiting_merge_col.get("class", [])
    assert awaiting_merge_col.select_one(".count").text.strip() == "1"
    pr_link = awaiting_merge_col.select_one(".card-head-actions .card-gh.card-pr-link")
    assert pr_link is not None
    assert pr_link.get("href") == "https://example.test/pr/7"
    assert pr_link.text.strip() == "PR ↗"
    assert pr_link.get("title") == "Open PR on GitHub"
    menu_btn = awaiting_merge_col.select_one(".card-menu-btn")
    assert menu_btn is not None
    assert menu_btn.get("data-pr-url") == "https://example.test/pr/7"


def test_completed_column_renders_merged_history_card(jinja_env):
    config = make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=7,
                title="Merged issue",
                agent_type="agent:web",
                status="merged",
                runtime_minutes=9,
                pr_url="https://example.test/pr/7",
                status_reason="PR merged; awaiting merge reconciled",
            )
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    awaiting_merge_col = soup.select_one('[data-column="awaiting-merge"]')
    completed_col = soup.select_one('[data-column="completed"]')
    assert awaiting_merge_col is not None
    assert completed_col is not None
    assert awaiting_merge_col.select_one(".count").text.strip() == "0"
    assert completed_col.select_one(".count").text.strip() == "1"
    completed_card = completed_col.select_one('.column-cards .issue-card[data-issue="7"]')
    assert completed_card is not None
    menu_btn = completed_card.select_one(".card-menu-btn")
    assert menu_btn is not None
    assert menu_btn.get("data-pr-url") == "https://example.test/pr/7"


def test_completed_card_phase_age_is_hydrated_by_dashboard_timestamp_localizer(jinja_env):
    """A completed card's phase-age is the UTC completion timestamp. It must be
    emitted with the shared [data-dashboard-timestamp] marker so the client
    localizes it to the viewer's timezone instead of showing raw GMT.
    """
    from datetime import timezone

    completed_at = datetime(2026, 6, 9, 1, 52, 59, tzinfo=timezone.utc)
    config = make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=392,
                title="Audit tixmeup",
                agent_type="agent:web",
                status="merged",
                runtime_minutes=9,
                pr_url="https://example.test/pr/392",
                status_reason="PR merged; awaiting merge reconciled",
                completed_at=completed_at,
            )
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    completed_card = soup.select_one('[data-column="completed"] .issue-card[data-issue="392"]')
    assert completed_card is not None
    age_el = completed_card.select_one(".card-phase-age")
    assert age_el is not None
    marker = age_el.select_one("[data-dashboard-timestamp]")
    assert marker is not None, "completed card phase-age must carry the localization marker"
    iso = completed_at.isoformat()
    assert marker["data-dashboard-timestamp"] == iso
    assert marker["data-dashboard-timestamp-fallback"] == "-"
    # Pre-hydration text is the raw source value; the localizer overwrites it.
    assert marker.get_text(strip=True) == iso
    # The separator lives outside the marked element so localization can't eat it.
    assert age_el.get_text(" ", strip=True).startswith("·")


def test_awaiting_merge_template_renders_one_pr_card_when_queue_and_history_overlap(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
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
                runtime_minutes=9,
                pr_url="https://example.test/pr/327",
            )
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    awaiting_merge_col = soup.select_one('[data-column="awaiting-merge"]')
    assert awaiting_merge_col is not None
    assert awaiting_merge_col.select_one(".count").text.strip() == "1"
    cards = awaiting_merge_col.select('.column-cards .issue-card[data-issue="280"]')
    assert len(cards) == 1
    pr_link = awaiting_merge_col.select_one(".card-head-actions .card-gh.card-pr-link")
    assert pr_link is not None
    assert pr_link.get("href") == "https://example.test/pr/327"


def test_status_badge_shows_running(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    badge = soup.select_one(".status-badge")
    assert badge is not None
    assert "Running" in badge.text


def test_issue_detail_drawer_is_rendered(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    drawer = soup.select_one("#issueDetailDrawer")
    assert drawer is not None
    assert drawer.get("role") == "dialog"
    assert drawer.get("aria-modal") == "true"
    assert drawer.get("aria-labelledby") == "issueDetailTitle"


def test_flow_refresh_preferences_modal_is_rendered(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    modal = soup.select_one("#flowRefreshPrefsModal")
    assert modal is not None
    assert "Flow refresh preferences" in soup.text
    assert soup.select_one("#flowRefreshOverrideEnabled") is not None
    assert soup.select_one("#flowFreshnessMode") is not None
    assert soup.select_one("#flowApiBudget") is not None
    assert soup.select_one("#flowAttentionPriority") is not None


def test_github_usage_pill_is_rendered(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    assert soup.select_one("#ghUsagePill") is not None
    assert soup.select_one("#ghUsagePanel") is not None
    embedded_pill = soup.select_one("#ghUsagePillEmbedded")
    embedded_panel = soup.select_one("#ghUsagePanelEmbedded")
    assert embedded_pill is not None
    assert embedded_panel is not None
    assert embedded_pill.get("aria-controls") == "ghUsagePanelEmbedded"
    show_usage_item = next(
        button
        for button in soup.select(".settings-menu-item")
        if "Show GitHub usage" in button.text
    )
    assert show_usage_item.get("onclick") == "showGitHubUsage()"


def test_embedded_header_elements_in_tab_bar(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    # Embedded header elements exist in the tab bar and are hidden by CSS
    # until the pre-paint boot state marks the page as embedded.
    tab_bar = soup.select_one(".dashboard-tabs")
    assert tab_bar.select_one("#embeddedBack") is not None
    assert tab_bar.select_one("#embeddedBackLabel") is not None
    assert tab_bar.select_one("#embeddedRepoName") is not None
    assert tab_bar.select_one("#embeddedBadge") is not None
    assert tab_bar.select_one("#embeddedScopeBtn") is not None
    assert "style" not in tab_bar.select_one("#embeddedBack").attrs


def test_starting_dashboard_renders_initializing_status(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="starting")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    init_status = soup.select_one("#dashboardInitStatus")
    assert init_status is not None
    assert "is-active" in init_status.get("class", [])
    assert "Initializing orchestrator" in init_status.get_text(" ")


def test_e2e_tab_and_panels_render(jinja_env):
    config = make_config()
    config.e2e.enabled = True
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="e2e",
        e2e_status_provider=lambda _: {
            "enabled": True,
            "running": False,
            "needs_attention": True,
            "untriaged_count": 2,
            "last_run": {"id": 9, "status": "failed", "relative_time": "3m ago"},
            "next_run": {"next_run_reason": "interval", "next_run_at": "2026-02-08T20:00:00Z"},
            "failed_tests": [],
        },
    )
    soup = render_dashboard(jinja_env, vm)
    assert soup.select_one("#tab-e2e.active") is not None
    assert soup.select_one("#panel-e2e") is not None
    assert soup.select_one("#e2eHeaderBadge") is not None
    assert soup.select_one("#e2eControls") is not None
    latest_results = soup.select_one(".e2e-last-results-btn")
    assert latest_results is not None
    assert latest_results.get_text(strip=True) == "View Results"
    assert latest_results.get("data-action") == "show-latest-e2e-run-results"
    assert "Last Run Diagnosis" not in soup.select_one("#panel-e2e").get_text(" ")
    # Issue #6334: the per-row "View Results" button is gone — rows are
    # ``<details>`` that expand inline.  The runs list mounts from the
    # typed ``RecentE2ERunsPayload`` embedded as inline JSON
    # (``#recentE2ERunsData``); ``e2e_runs_list.js`` reads it on
    # DOMContentLoaded.
    runs_list_root = soup.select_one("#e2eRunsListRoot")
    assert runs_list_root is not None
    runs_list_data = soup.select_one('#recentE2ERunsData[type="application/json"]')
    assert runs_list_data is not None


def test_e2e_tab_disables_results_action_when_no_run_exists(jinja_env):
    config = make_config()
    config.e2e.enabled = True
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="e2e",
        e2e_status_provider=lambda _: {
            "enabled": True,
            "running": False,
            "needs_attention": False,
            "untriaged_count": 0,
            "failed_tests": [],
        },
    )
    soup = render_dashboard(jinja_env, vm)

    latest_results = soup.select_one(".e2e-last-results-btn")
    assert latest_results is not None
    assert latest_results.get_text(strip=True) == "No Results"
    assert latest_results.has_attr("disabled")
    assert latest_results.get("onclick") is None


def test_server_rendered_card_carries_fingerprint_for_first_paint_no_flash(jinja_env):
    """When the first refreshViewModel runs after DOMContentLoaded, the JS
    diff (`existing.dataset.cardFingerprint !== nextFingerprint`) decides
    whether to keep or replace each card. The server MUST stamp the same
    fingerprint string the JS will compute, otherwise every card on the
    page is replaced once on open and the user sees a flash.
    """
    from issue_orchestrator.view_models.dashboard_flow import (
        compute_compact_card_fingerprint,
    )

    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    issue = Issue(number=101, title="Ship board", labels=["agent:web"])
    state = OrchestratorState(
        startup_status="complete",
        active_sessions=[make_session(issue)],
        cached_queue_issues=[Issue(number=102, title="Queued item", labels=["agent:web"])],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)

    cards = soup.select(".issue-card[data-issue]")
    assert cards, "expected at least one server-rendered issue card"
    for card_el in cards:
        assert card_el.get("data-card-id"), (
            "card missing data-card-id; JS lookup falls back to issue-N but "
            "explicit id avoids drift if card_id ever differs"
        )
        assert card_el.get("data-card-fingerprint"), (
            "card missing data-card-fingerprint; first JS refresh will replace "
            "every card on open and re-introduce the dashboard flash"
        )
        # The phase line must be split into separate spans so the JS can
        # update phase_age in place without replacing the whole card.
        assert card_el.select_one(".card-phase-text") is not None
        assert card_el.select_one(".card-phase-age") is not None


def test_server_rendered_fingerprint_matches_js_helper_output(jinja_env):
    """Cross-check: the fingerprint stamped on the DOM must equal what the
    JS helper would compute for the *same* card payload the server passed
    to the template. If this drifts, every initial card-replacement
    returns and the flash returns with it.
    """
    import json
    import subprocess
    import textwrap

    config = make_config()
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[Issue(number=4242, title="Render cleanly", labels=["agent:web"])],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    card_el = soup.select_one(".issue-card[data-issue]")
    assert card_el is not None
    server_fp = card_el.get("data-card-fingerprint")

    # Pluck the exact card dict the template received so the JS helper sees
    # the same payload — anything else risks false positives or negatives.
    ctx = vm.template_context()
    card = next(
        (
            c
            for column in ctx["flow_columns"]
            for c in column["items"]
            if c.get("issue_number") == 4242
        ),
        None,
    )
    assert card is not None, "expected the queued card in flow_columns"

    helper_path = (
        Path(__file__).resolve().parents[2]
        / "src" / "issue_orchestrator" / "static" / "js" / "compact_card_state.js"
    )
    js = textwrap.dedent(
        f"""
        const helper = require('{helper_path}');
        const card = JSON.parse(process.argv[1]);
        process.stdout.write(helper.computeCompactCardFingerprint(card));
        """
    )
    js_fp = subprocess.run(
        ["node", "-e", js, json.dumps(card)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert server_fp == js_fp


def test_first_paint_active_card_menu_carries_run_dir(jinja_env):
    """F1 (#6732): the server template must stamp `data-run-dir` on the active
    compact card menu button. The card fingerprint already includes run_dir, so
    an active card present on first paint keeps its reused DOM node on the first
    client refresh instead of being replaced by JS-rendered HTML. If the server
    node lacks `data-run-dir`, the "View Agent Prompt" action reads an empty
    runDir indefinitely and falls back to the static agent template instead of
    the run-scoped launch prompt this issue fixes. Complements the JS renderer
    test so both render paths stay in parity.
    """
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    running_issue = Issue(number=4057, title="Running issue", labels=["agent:web", "in-progress"])
    state = OrchestratorState(startup_status="complete", active_sessions=[make_session(running_issue)])
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    # The active card's run_dir is what the menu button must carry verbatim.
    card = next(
        c
        for column in vm.template_context()["flow_columns"]
        for c in column["items"]
        if c.get("issue_number") == 4057
    )
    expected_run_dir = card["run_dir"]
    assert expected_run_dir, "active session card should expose a non-empty run_dir"

    soup = render_dashboard(jinja_env, vm)

    running_col = soup.select_one('[data-column="running"]')
    assert running_col is not None
    menu_btn = running_col.select_one(".card-menu-btn")
    assert menu_btn is not None
    assert menu_btn.get("data-run-dir") == expected_run_dir
