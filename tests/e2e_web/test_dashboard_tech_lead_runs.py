"""Requesting tech-lead runs from the RUNNING dashboard (#6994 round 1 F8).

Everything below this file is unit- or vm-level: it proves the admission matrix,
the projection, and the JS rendering in isolation. What it cannot prove is that
those pieces are wired to each other in a live dashboard — that a click reaches
``POST /api/tech-lead/runs``, that the answer changes the view model the server
publishes, and that the next refresh renders the new state on every affordance.
That whole chain is what these tests exercise, in a real browser against a real
dashboard app.

Determinism: the engine behind the dashboard is a real
:class:`TechLeadRunCoordinator` over in-memory state and the single-instance run
claim store — no GitHub, no agent processes, no clocks. Requests really are
admitted, really do enqueue, and the projection really is recomputed from the
resulting state, so nothing here is a stub asserting against itself.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Page, expect

from issue_orchestrator.control.tech_lead_run_admission import TechLeadRunCoordinator
from issue_orchestrator.control.tech_lead_run_ownership import TechLeadRunOwnership
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    Issue,
    PendingTechLeadReview,
    Session,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_run import TechLeadRunAdmission
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.entrypoints import web as web_module
from issue_orchestrator.execution.session_output_adapter import (
    FileSystemSessionOutput,
)
from issue_orchestrator.ports.run_ledger_store import (
    SingleInstanceRunLedgerStore,
)
from tests.e2e_web.conftest import (
    FlowWebMockOrchestrator,
    UvicornTestServer,
    _configure_flow_deps,
    find_free_port,
)

TECH_LEAD_AGENT = "agent:tech-lead"
BLOCKED_ISSUE = 177
ANCHOR = 900


class TechLeadWebOrchestrator(FlowWebMockOrchestrator):
    """A dashboard-served engine whose admission decisions are the real ones."""

    def __init__(self) -> None:
        super().__init__()
        self.config.tech_lead_review_agent = TECH_LEAD_AGENT
        self.issues: dict[int, Issue] = {}
        self._ownership = TechLeadRunOwnership(
            SingleInstanceRunLedgerStore(lease_seconds=900),
            lease_seconds=900,
            renew_before_expiry_seconds=300,
        )
        self._repo_root = Path("/tmp/repo")

    def bind_repo_root(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    # -- the anchor lifecycle, minus GitHub -----------------------------
    def ensure_health_review_anchor(self) -> PendingTechLeadReview:
        item = PendingTechLeadReview(
            ANCHOR,
            "Health Review — walk the floor",
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        )
        self.state.pending_tech_lead_reviews.append(item)
        return item

    def get_issue(self, number: int) -> Issue | None:
        return self.issues.get(number)

    def request_tech_lead_run(self, request) -> TechLeadRunAdmission:
        return TechLeadRunCoordinator(
            state=self.state,
            config=self.config,
            repository_host=SimpleNamespace(get_issue=self.get_issue),
            anchor_host=self,
            ownership=self._ownership,
            is_blocking_any=lambda labels: any(
                str(label).startswith("blocked") for label in labels
            ),
            events=SimpleNamespace(publish=lambda _event: None),
        ).admit(request)

    # -- test affordances -----------------------------------------------
    def start_global_run(self) -> None:
        """Promote the queued health review into a RUNNING global session."""
        self.state.pending_tech_lead_reviews = [
            item
            for item in self.state.pending_tech_lead_reviews
            if item.flavor is not TechLeadSessionFlavor.HEALTH_REVIEW
        ]
        anchor = Issue(number=ANCHOR, title="Health Review", labels=[TECH_LEAD_AGENT])
        session_name = f"tech-lead-{ANCHOR}"
        worktree_path = self._repo_root / "worktrees" / session_name
        run_assets = FileSystemSessionOutput().start_run(
            worktree_path=worktree_path,
            session_name=session_name,
            issue_number=ANCHOR,
            agent_label=TECH_LEAD_AGENT,
            backend="fixture",
        )
        self.state.active_sessions.append(
            Session(
                key=SessionKey(
                    issue=GitHubIssueKey(repo="test/repo", external_id=str(ANCHOR)),
                    task=TaskKind.CODE,
                ),
                issue=anchor,
                agent_config=self.config.agents["agent:web"],
                terminal_id=session_name,
                worktree_path=worktree_path,
                branch_name="main",
                run_assets=run_assets,
                agent_label=TECH_LEAD_AGENT,
                # The producer's grant — the same stamp the restorer rebuilds
                # after a restart, and the reason the barrier survives one.
                tech_lead_scope=TechLeadLaunchScope(
                    flavor=TechLeadSessionFlavor.HEALTH_REVIEW
                ),
            )
        )


@pytest.fixture(scope="module")
def tech_lead_server(tmp_path_factory: pytest.TempPathFactory):
    orchestrator = TechLeadWebOrchestrator()
    repo_root = tmp_path_factory.mktemp("tech-lead-dashboard-repo")
    _configure_flow_deps(orchestrator, repo_root)
    orchestrator.bind_repo_root(repo_root)
    orchestrator.add_queue_issue(
        BLOCKED_ISSUE, "Blocked merge item", labels=["agent:web", "blocked-needs-human"]
    )
    orchestrator.issues[BLOCKED_ISSUE] = Issue(
        number=BLOCKED_ISSUE,
        title="Blocked merge item",
        labels=["agent:web", "blocked-needs-human"],
    )
    port = find_free_port()
    web_module.configure_dashboard_admin_token(None)
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)
    server = UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {"url": f"http://127.0.0.1:{port}", "orchestrator": orchestrator}
    finally:
        server.stop()
        web_module.set_orchestrator(original)


def _open(page: Page, server) -> None:
    page.goto(str(server["url"]), wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def _global_status(page: Page) -> str:
    return page.evaluate("() => window.dashboardData.techLeadRuns.globalStatus")


def _reset(orchestrator: TechLeadWebOrchestrator) -> None:
    orchestrator.state.pending_tech_lead_reviews.clear()
    orchestrator.state.active_sessions.clear()


def test_the_global_action_requests_a_run_and_the_dashboard_reports_it_queued(
    page: Page, tech_lead_server
) -> None:
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    _open(page, tech_lead_server)

    assert _global_status(page) == "idle"
    page.click("#settingsMenuBtn")
    item = page.locator("#techLeadHealthReviewItem")
    expect(item).to_be_enabled()
    item.click()

    # The engine really admitted it...
    expect(page.locator("#toast")).to_contain_text("health review")
    assert [i.issue_number for i in orchestrator.state.pending_tech_lead_reviews] == [
        ANCHOR
    ]
    # ...and the dashboard reports the truth without a manual reload.
    page.wait_for_function(
        "() => window.dashboardData.techLeadRuns.globalStatus === 'queued'",
        timeout=10_000,
    )
    expect(page.locator("#techLeadHealthReviewStatus")).to_have_text("Tech lead queued")
    # aria-disabled, NOT the native property: the control must stay focusable so
    # its reason is reachable by keyboard (#6994 round 2 F6).
    expect(page.locator("#techLeadHealthReviewItem")).to_have_attribute(
        "aria-disabled", "true"
    )


def test_a_repeated_global_request_coalesces_instead_of_queueing_twice(
    page: Page, tech_lead_server
) -> None:
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    _open(page, tech_lead_server)

    page.click("#settingsMenuBtn")
    page.click("#techLeadHealthReviewItem")
    page.wait_for_function(
        "() => window.dashboardData.techLeadRuns.globalStatus === 'queued'",
        timeout=10_000,
    )
    # The affordance is now disabled, so the operator's second attempt goes
    # through the same command surface rather than the (disabled) button.
    status = page.evaluate(
        """async () => {
            const res = await fetch('/api/tech-lead/runs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: { kind: 'global_health_review' } }),
            });
            return (await res.json()).outcome;
        }"""
    )

    assert status == "already_queued"
    assert len(orchestrator.state.pending_tech_lead_reviews) == 1


def test_the_queued_state_survives_a_dashboard_refresh(
    page: Page, tech_lead_server
) -> None:
    """A reload re-derives the affordance from the server, not from memory."""
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    _open(page, tech_lead_server)
    page.click("#settingsMenuBtn")
    page.click("#techLeadHealthReviewItem")
    page.wait_for_function(
        "() => window.dashboardData.techLeadRuns.globalStatus === 'queued'",
        timeout=10_000,
    )

    _open(page, tech_lead_server)

    assert _global_status(page) == "queued"
    page.click("#settingsMenuBtn")
    expect(page.locator("#techLeadHealthReviewItem")).to_have_attribute(
        "aria-disabled", "true"
    )
    expect(page.locator("#techLeadHealthReviewStatus")).to_have_text("Tech lead queued")


def test_a_running_global_run_is_reported_as_running_after_a_restart(
    page: Page, tech_lead_server
) -> None:
    """The barrier survives the engine restart the restorer rebuilds (F3)."""
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    orchestrator.start_global_run()

    _open(page, tech_lead_server)

    assert _global_status(page) == "running"
    assert page.evaluate(
        "() => window.dashboardData.techLeadRuns.globalBarrierActive"
    )
    # The anchor is not a board card, so it must not read as a targeted run.
    assert page.evaluate(
        "() => window.dashboardData.techLeadRuns.runningIssueNumbers"
    ) == []


def test_a_targeted_request_is_queued_behind_an_active_global_run(
    page: Page, tech_lead_server
) -> None:
    """Global-before-targeted ordering, observed from the dashboard."""
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    orchestrator.start_global_run()
    _open(page, tech_lead_server)

    payload = page.evaluate(
        """async () => {
            const res = await fetch('/api/tech-lead/runs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: { kind: 'issue', issue_number: %d } }),
            });
            return await res.json();
        }"""
        % BLOCKED_ISSUE
    )

    assert payload["outcome"] == "queued"
    assert payload["behind_global_barrier"] is True

    page.evaluate("() => refreshViewModel()")
    page.wait_for_function(
        "() => (window.dashboardData.techLeadRuns.queuedIssueNumbers || []).length === 1",
        timeout=10_000,
    )
    assert page.evaluate(
        "() => window.dashboardData.techLeadRuns.queuedIssueNumbers"
    ) == [BLOCKED_ISSUE]


def test_a_stopped_engine_answers_the_command_surface_with_a_typed_refusal(
    page: Page, tech_lead_server
) -> None:
    """The dashboard must never promise a run nothing would start (F5).

    Asserted from the live page's own fetch, so the browser really does receive
    a body it can branch on rather than an ad hoc error shape. How the UI then
    renders that state is pinned at the JS-vm layer ("a stopped engine says so
    instead of blaming configuration"), which is cheaper and exhaustive.
    """
    _reset(tech_lead_server["orchestrator"])
    _open(page, tech_lead_server)
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(None)
    try:
        status, body = page.evaluate(
            """async () => {
                const res = await fetch('/api/tech-lead/runs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ scope: { kind: 'global_health_review' } }),
                });
                return [res.status, await res.json()];
            }"""
        )
    finally:
        web_module.set_orchestrator(original)

    assert status == 503
    assert body["outcome"] == "not_running"
    assert body["reason"] == "engine_not_running"
    assert body["admitted"] is False
    assert body["run_key"] == "global:health_review"


# ---------------------------------------------------------------------------
# Accessibility of the tech-lead affordances in a REAL browser (#6994 R2 F6)
#
# The JS-vm layer pins the rendering rules exhaustively and cheaply; what only a
# browser can prove is that the resulting DOM is valid and operable — that the
# control is in the tab order, that its description resolves to an element that
# exists, and that the Settings remedy is a sibling rather than a nested button.
# ---------------------------------------------------------------------------


def _health_action_a11y(page: Page) -> dict:
    return page.evaluate(
        """() => {
            const item = document.getElementById('techLeadHealthReviewItem');
            const describedBy = item.getAttribute('aria-describedby');
            const described = describedBy ? document.getElementById(describedBy) : null;
            const link = document.getElementById('techLeadHealthReviewItemSettingsLink');
            return {
                ariaDisabled: item.getAttribute('aria-disabled'),
                nativelyDisabled: item.disabled,
                describedBy,
                describedText: described ? described.textContent : null,
                describedIsChildOfButton: described ? item.contains(described) : null,
                nestedButtons: item.querySelectorAll('button').length,
                remedyIsChildOfButton: link ? item.contains(link) : null,
                remedyPresent: Boolean(link),
            };
        }"""
    )


def test_a_queued_global_run_stays_keyboard_operable_with_a_resolvable_reason(
    page: Page, tech_lead_server
) -> None:
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    _open(page, tech_lead_server)
    page.click("#settingsMenuBtn")
    page.click("#techLeadHealthReviewItem")
    page.wait_for_function(
        "() => window.dashboardData.techLeadRuns.healthReviewStatus === 'queued'",
        timeout=10_000,
    )
    # Requesting a run closes the actions menu; reopen it to inspect the
    # affordance as the operator would next see it.
    page.click("#settingsMenuBtn")

    a11y = _health_action_a11y(page)

    assert a11y["ariaDisabled"] == "true"
    assert a11y["nativelyDisabled"] is False, "an unfocusable control hides its reason"
    assert a11y["describedText"] == "Tech lead queued"
    assert a11y["describedIsChildOfButton"] is False
    assert a11y["nestedButtons"] == 0
    # Focusable in the real browser, not merely styled that way.
    page.focus("#techLeadHealthReviewItem")
    assert page.evaluate(
        "() => document.activeElement.id"
    ) == "techLeadHealthReviewItem"


def test_a_running_global_run_reports_its_state_on_the_action(
    page: Page, tech_lead_server
) -> None:
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    orchestrator.start_global_run()
    _open(page, tech_lead_server)
    page.click("#settingsMenuBtn")

    expect(page.locator("#techLeadHealthReviewStatus")).to_have_text(
        "Tech lead running"
    )
    a11y = _health_action_a11y(page)
    assert a11y["ariaDisabled"] == "true"
    assert a11y["nativelyDisabled"] is False
    assert a11y["nestedButtons"] == 0


def test_an_unconfigured_engine_offers_a_sibling_settings_remedy_in_the_browser(
    page: Page, tech_lead_server
) -> None:
    """The remedy must be operable markup, not a button inside a button."""
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    original_agent = orchestrator.config.tech_lead_review_agent
    orchestrator.config.tech_lead_review_agent = ""
    try:
        _open(page, tech_lead_server)
        page.click("#settingsMenuBtn")
        a11y = _health_action_a11y(page)

        assert a11y["remedyPresent"] is True
        assert a11y["remedyIsChildOfButton"] is False
        assert a11y["nestedButtons"] == 0
        assert "No tech lead agent is configured" in (a11y["describedText"] or "")
        page.focus("#techLeadHealthReviewItemSettingsLink")
        assert page.evaluate(
            "() => document.activeElement.id"
        ) == "techLeadHealthReviewItemSettingsLink"
    finally:
        orchestrator.config.tech_lead_review_agent = original_agent


def test_a_paused_engine_disables_the_action_but_keeps_its_reason_reachable(
    page: Page, tech_lead_server
) -> None:
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    orchestrator.state.paused = True
    try:
        _open(page, tech_lead_server)
        page.click("#settingsMenuBtn")
        a11y = _health_action_a11y(page)

        assert a11y["ariaDisabled"] == "true"
        assert a11y["nativelyDisabled"] is False
        assert "paused" in (a11y["describedText"] or "").lower()
        assert a11y["remedyPresent"] is False, "a pause is not a Settings problem"
    finally:
        orchestrator.state.paused = False


def test_a_queued_BATCH_review_still_lets_the_operator_request_a_health_review(
    page: Page, tech_lead_server
) -> None:
    """Distinct global flavors serialize; they do not suppress each other (F5)."""
    orchestrator = tech_lead_server["orchestrator"]
    _reset(orchestrator)
    orchestrator.state.pending_tech_lead_reviews.append(
        PendingTechLeadReview(
            800, "Batch Review", flavor=TechLeadSessionFlavor.BATCH_REVIEW
        )
    )
    try:
        _open(page, tech_lead_server)
        page.click("#settingsMenuBtn")

        assert _global_status(page) == "queued"
        a11y = _health_action_a11y(page)
        assert a11y["ariaDisabled"] == "false", (
            "a batch review must not make the health action look already-requested"
        )
        assert "will start after it" in (a11y["describedText"] or "")

        page.click("#techLeadHealthReviewItem")
        page.wait_for_function(
            "() => window.dashboardData.techLeadRuns.healthReviewStatus === 'queued'",
            timeout=10_000,
        )
        assert sorted(
            i.issue_number for i in orchestrator.state.pending_tech_lead_reviews
        ) == [800, ANCHOR]
    finally:
        _reset(orchestrator)
