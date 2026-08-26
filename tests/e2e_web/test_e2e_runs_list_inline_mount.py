"""Playwright smoke for the inline E2E runs-as-rows list (issue #6334).

Pins three things in the live pipeline (template → JS chunk → typed
Command → dispatcher → lazy fetch → canonical viewer mount):

  1. The Run History panel renders one ``<details class="e2e-run-row">``
     per recent E2E run, each carrying a typed ``expand_e2e_run``
     Command in ``data-lifecycle-command`` and the shared
     ``runLifecycleCommandFromToggle`` dispatcher hook.
  2. Clicking a row's summary triggers a lazy fetch of
     ``/api/e2e-run-detail/{run_id}`` and mounts the canonical
     viewer (``.cvv-root``) inside the row body — no modal teleport.
  3. ``#e2eDiagnosisModal`` is NOT in the DOM at all.

The matrix of per-tone rendering, dispatcher branches, and predictable-
collapse is covered by ``tests/js/e2e_runs_list.test.js``; this is the
end-to-end live-pipeline proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from typing import Protocol

from playwright.sync_api import Page, Route, expect


_RUN_ID = 7777


class _TwoAgentWebServer(Protocol):
    """Typed fixture surface needed by the row-scoping smoke test."""

    url: str


@dataclass(frozen=True, slots=True)
class _CreateIssuesRequest:
    """Strict request captured by the row-scoping browser smoke test."""

    agent: str
    nodeids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.agent) is not str or not self.agent:
            raise ValueError("_CreateIssuesRequest.agent must not be empty")
        if type(self.nodeids) is not tuple or not self.nodeids or any(
            type(nodeid) is not str or not nodeid for nodeid in self.nodeids
        ):
            raise ValueError(
                "_CreateIssuesRequest.nodeids must contain non-empty strings"
            )

    @classmethod
    def from_json(cls, payload: str) -> _CreateIssuesRequest:
        decoded: object = json.loads(payload)
        if type(decoded) is not dict:
            raise ValueError("create-issues request must be a JSON object")
        agent = decoded.get("agent")
        nodeids = decoded.get("nodeids")
        if type(agent) is not str:
            raise ValueError("create-issues request agent must be a string")
        if type(nodeids) is not list or any(
            type(nodeid) is not str for nodeid in nodeids
        ):
            raise ValueError(
                "create-issues request nodeids must be an array of strings"
            )
        return cls(agent, tuple(nodeids))


@dataclass(frozen=True, slots=True)
class _CapturedCreateIssuesCall:
    """URL plus its strongly typed create-issues request."""

    url: str
    request: _CreateIssuesRequest

    def __post_init__(self) -> None:
        if type(self.url) is not str or not self.url:
            raise ValueError("_CapturedCreateIssuesCall.url must not be empty")
        if type(self.request) is not _CreateIssuesRequest:
            raise ValueError(
                "_CapturedCreateIssuesCall.request must be _CreateIssuesRequest"
            )


_STUB_RUN_DETAIL: dict[str, object] = {
    "run": {
        "id": _RUN_ID,
        "status": "failed",
        "started_at": "2026-05-12T01:00:00Z",
        "ended_at": "2026-05-12T01:01:00Z",
        "duration_seconds": 60.0,
        "commit_sha": "abc1234",
        "branch": "main",
        "runner_kind": "pytest",
        "command": ["pytest", "tests/e2e", "--junit-xml=junit.xml"],
    },
    "results_by_category": {
        "untriaged": [
            {
                "nodeid": "tests/e2e/test_a.py::test_untracked_failure",
                "suite_name": "tests/e2e/test_a.py",
                "outcome": "failed",
                "duration_seconds": 30.0,
                "failure_summary": "TimeoutError: orchestrator did not publish within 30s",
                "longrepr": "",
                "history": [],
                "existing_issue": None,
                "is_quarantined": False,
                "result_source": "junit_xml",
            },
        ],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [
            {"nodeid": "tests/e2e/test_c.py::test_one", "suite_name": "tests/e2e/test_c.py", "outcome": "passed", "duration_seconds": 0.1},
        ],
        "quarantined": [],
        "skipped": [],
    },
    "results_summary": {"total": 2, "passed": 1, "failed": 1, "skipped": 0, "untriaged": 1, "has_issue": 0, "flaky": 0, "fixed": 0, "quarantined": 0},
    "artifacts": [],
    "reports": [],
    "issue_affordances": [],
    "lifecycle": None,
    "events": [],
    "phase_toc": [],
    "cycles": [],
}


_STUB_RECENT_RUNS_PAYLOAD: dict[str, object] = {
    "runs": [
        {
            "run_id": _RUN_ID,
            "outcome": {"label": "Failed", "tone": "failed"},
            "started_at": "2026-05-12T01:00:00Z",
            "finished_at": "2026-05-12T01:01:00Z",
            "duration_seconds": 60.0,
            "commit_sha": "abc1234",
            "branch": "main",
            "runner_kind": "pytest",
            "command_summary": "pytest tests/e2e --junit-xml=junit.xml",
            "results": {
                "passed": 1, "failed": 1, "errored": 0,
                "skipped": 0, "quarantined": 0, "total": 2,
            },
            "note": None,
            "expand_command": {
                "kind": "expand_e2e_run",
                "label": "Expand E2E Run",
                "run_id": _RUN_ID,
            },
        },
        {
            "run_id": _RUN_ID + 1,
            "outcome": {"label": "Passed", "tone": "passed"},
            "started_at": "2026-05-11T01:00:00Z",
            "finished_at": "2026-05-11T01:00:30Z",
            "duration_seconds": 30.0,
            "commit_sha": "deadbeef",
            "branch": "main",
            "runner_kind": "pytest",
            "command_summary": "pytest tests/e2e",
            "results": {
                "passed": 36, "failed": 0, "errored": 0,
                "skipped": 0, "quarantined": 0, "total": 36,
            },
            "note": None,
            "expand_command": {
                "kind": "expand_e2e_run",
                "label": "Expand E2E Run",
                "run_id": _RUN_ID + 1,
            },
        },
    ],
}


def _goto_dashboard_e2e_tab(page: Page, base_url: str) -> None:
    # The runs-list mount point lives inside the E2E panel; navigate
    # straight to ``?tab=e2e`` so the runs list root is in the DOM
    # on first paint.
    page.goto(f"{base_url}/?tab=e2e", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def _inject_runs_list(page: Page, payload: dict[str, object]) -> None:
    page.evaluate(
        """(payload) => {
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }""",
        payload,
    )


def _many_recent_runs_payload(count: int = 19) -> dict[str, object]:
    runs: list[dict[str, object]] = []
    for offset in range(count):
        run_id = _RUN_ID + offset
        failed = offset in {0, 7, 11}
        started_at = datetime(2026, 5, 12, 1, 0, 0, tzinfo=timezone.utc) - timedelta(days=offset)
        finished_at = started_at + timedelta(minutes=3)
        runs.append(
            {
                "run_id": run_id,
                "outcome": {
                    "label": "Failed" if failed else "Passed",
                    "tone": "failed" if failed else "passed",
                },
                "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_seconds": 199.7 + offset,
                "commit_sha": f"{run_id:07x}",
                "branch": "HEAD",
                "runner_kind": "pytest",
                "command_summary": "sh scripts/run-issue-orchestrator-suite.sh",
                "results": {
                    "passed": 4 if failed else 5,
                    "failed": 1 if failed else 0,
                    "errored": 0,
                    "skipped": 0,
                    "quarantined": 0,
                    "total": 5,
                },
                "note": None,
                "expand_command": {
                    "kind": "expand_e2e_run",
                    "label": "Expand E2E Run",
                    "run_id": run_id,
                },
            }
        )
    return {"runs": runs}


def test_inline_runs_list_renders_rows_and_mounts_canonical_viewer_on_expand(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Click a row, prove the canonical viewer mounts inline (#6334).

    Asserts the issue body's Playwright-smoke spec:
      * N rows render
      * click one row's summary
      * the canonical viewer mounts inline with the correct run id
      * ``#e2eDiagnosisModal`` is not in the DOM at all
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Stub the per-run detail endpoint so the lazy-fetch on expand
    # resolves to a deterministic fixture without hitting the real
    # backend.
    page.route(
        f"**/api/e2e-run-detail/{_RUN_ID}**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RUN_DETAIL),
        ),
    )

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    # The default ``web_server`` fixture's mock orchestrator has E2E
    # disabled, so the E2E panel (and the runs-list root) aren't in
    # the SSR HTML.  Inject the mount points + stubbed payload, then
    # call the renderer directly — the production code path is
    # exactly this (``renderE2ERunsList`` reads typed payload, mounts
    # into ``#e2eRunsListRoot``).
    _inject_runs_list(page, _STUB_RECENT_RUNS_PAYLOAD)

    # The modal is gone — assert it's not in the DOM at all (the
    # issue body's explicit guardrail).  Note: ``to_have_count(0)``
    # passes for "element not found" so this is the sticky way to
    # prove the modal markup is dropped.
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    # ── 1. N rows render ─────────────────────────────────────────────
    rows = page.locator("details.e2e-run-row")
    expect(rows).to_have_count(2, timeout=10_000)
    # Each row carries the typed Command in data-lifecycle-command +
    # the shared toggle dispatcher (single-owner contract).
    first_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID}']")
    expect(first_row).to_have_count(1)
    cmd_raw = first_row.get_attribute("data-lifecycle-command") or ""
    assert cmd_raw, "row must carry data-lifecycle-command"
    cmd = json.loads(cmd_raw.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd == {
        "kind": "expand_e2e_run",
        "label": "Expand E2E Run",
        "run_id": _RUN_ID,
    }, f"unexpected typed Command on row: {cmd!r}"
    ontoggle = first_row.get_attribute("ontoggle") or ""
    assert "runLifecycleCommandFromToggle" in ontoggle, (
        f"row ontoggle must route through the shared dispatcher; got {ontoggle!r}"
    )

    # Closed by default — predictable-collapse rule from issue #6322.
    assert first_row.evaluate("el => el.open") is False

    # ── 2. Click the row's summary to expand it ──────────────────────
    first_row.locator("summary").click()
    expect(first_row).to_have_js_property("open", True)

    # ── 3. Canonical viewer mounts inline with the right run id ──────
    # ``loadE2ERunIntoRow`` fetches ``/api/e2e-run-detail/{run_id}`` and
    # mounts ``renderE2EResultsPanel(data)`` inside ``.e2e-run-row-content``.
    cvv = first_row.locator(".cvv-root")
    expect(cvv).to_be_visible(timeout=10_000)
    expect(cvv).to_have_attribute("data-cvv-status", "failed")

    # Run-level summary chips show the right counts from the lazy
    # fetch — proves we mounted THIS run's data, not a different one.
    summary = first_row.locator(".e2e-run-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("failed")
    expect(summary).to_contain_text("2 cases")
    expect(summary).to_contain_text("1 failing")
    expect(summary).to_contain_text("1 passing")

    # ── 4. Re-collapsing the row keeps the cache (predictable-collapse) ─
    # ``data-loaded`` flips to '1' on first open; closing + reopening
    # must not re-fetch.  We can't observe the network easily here,
    # so test the marker.
    assert first_row.evaluate("el => el.dataset.loaded") == "1"

    # ── 5. The other row is still closed and has its own typed Command ─
    second_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID + 1}']")
    assert second_row.evaluate("el => el.open") is False
    cmd2_raw = second_row.get_attribute("data-lifecycle-command") or ""
    cmd2 = json.loads(cmd2_raw.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd2["run_id"] == _RUN_ID + 1

    # No page errors during the run.
    assert not errors, f"unexpected page errors: {errors}"


def test_run_history_rows_are_not_clipped_and_expanded_list_uses_page_scroll(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """The Run History list must not become a cramped nested scroller.

    This is layout-dependent, so it belongs in Playwright: the old
    ``cards.css`` rule capped ``.e2e-runs-list`` at ``65vh`` and made
    expanded rows feel clipped.  The run-history stylesheet now owns
    the list layout explicitly and lets the page scroll.
    """
    page.set_viewport_size({"width": 1600, "height": 800})
    page.route(
        f"**/api/e2e-run-detail/{_RUN_ID}**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RUN_DETAIL),
        ),
    )

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    _inject_runs_list(page, _many_recent_runs_payload())

    first_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID}']")
    first_summary = first_row.locator("summary.e2e-run-row-summary")
    first_summary.click()
    expect(first_row.locator(".cvv-root")).to_be_visible(timeout=10_000)
    page.evaluate(
        """() => {
            const root = document.getElementById('e2eRunsListRoot');
            const sentinel = document.createElement('button');
            sentinel.type = 'button';
            sentinel.id = 'e2eRunHistoryFocusSentinel';
            sentinel.textContent = 'Focus sentinel';
            root.parentElement.insertBefore(sentinel, root);
            sentinel.focus();
        }"""
    )
    page.keyboard.press("Tab")
    expect(first_summary).to_be_focused()

    metrics = page.evaluate(
        """() => {
            const list = document.querySelector('.e2e-runs-list');
            const rows = Array.from(document.querySelectorAll('details.e2e-run-row'));
            const summaries = rows.map((row) => row.querySelector('summary'));
            const listStyle = getComputedStyle(list);
            const closedSummaryHeights = summaries.slice(1).map((summary) =>
                summary.getBoundingClientRect().height
            );
            const clippedChildren = summaries.some((summary) => {
                const summaryRect = summary.getBoundingClientRect();
                return Array.from(summary.children).some((child) => {
                    const childRect = child.getBoundingClientRect();
                    return childRect.top < summaryRect.top - 0.5
                        || childRect.bottom > summaryRect.bottom + 0.5;
                });
            });
            const focusStyle = getComputedStyle(summaries[0]);
            const listRect = list.getBoundingClientRect();
            return {
                listOverflowY: listStyle.overflowY,
                listMaxHeight: listStyle.maxHeight,
                listHeight: listRect.height,
                listClientHeight: list.clientHeight,
                listScrollHeight: list.scrollHeight,
                viewportHeight: window.innerHeight,
                minClosedSummaryHeight: Math.min(...closedSummaryHeights),
                clippedChildren,
                firstSummaryFocused: document.activeElement === summaries[0],
                firstSummaryControls: summaries[0].getAttribute('aria-controls'),
                firstBodyRole: rows[0].querySelector('.e2e-run-row-body').getAttribute('role'),
                firstBodyLabel: rows[0].querySelector('.e2e-run-row-body').getAttribute('aria-labelledby'),
                focusOutlineStyle: focusStyle.outlineStyle,
                focusOutlineWidth: focusStyle.outlineWidth,
            };
        }"""
    )

    assert metrics["listOverflowY"] == "visible"
    assert metrics["listMaxHeight"] == "none"
    assert metrics["listHeight"] > metrics["viewportHeight"] * 0.75
    assert abs(metrics["listScrollHeight"] - metrics["listClientHeight"]) <= 2
    assert metrics["minClosedSummaryHeight"] >= 44
    assert metrics["clippedChildren"] is False
    assert metrics["firstSummaryFocused"] is True
    assert metrics["firstSummaryControls"] is None
    assert metrics["firstBodyRole"] is None
    assert metrics["firstBodyLabel"] is None
    assert metrics["focusOutlineStyle"] != "none"
    assert metrics["focusOutlineWidth"] != "0px"


def test_two_rows_expanded_act_independently(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Issue #6334 round-2 reviewer blocker: two rows expanded at the
    same time must NOT share state.

    The legacy ``unifiedRunData`` module-level singleton broke as
    soon as a second row could be expanded.  The fix moved
    ownership to the row itself (``row._e2eRunData``) and emits
    typed Commands (``switch_e2e_timeline_view``,
    ``create_e2e_untriaged_issues``) that carry the row's
    ``run_id`` explicitly.

    This live-browser smoke verifies:

      1. Row A and Row B both load their OWN detail payload (no
         shared state).
      2. Clicking a Story/Ops/Debug button inside Row A fetches
         row A's run id with the chosen view, leaves Row B's
         timeline untouched.
      3. The two rows' Diagnostics disclosures and
         timeline containers are independent — no document-global
         id collisions.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    run_a_id = _RUN_ID
    run_b_id = _RUN_ID + 1
    # Per-run detail payloads — distinct enough to detect cross-row
    # contamination if it happens.
    detail_a = {**_STUB_RUN_DETAIL, "run": {**_STUB_RUN_DETAIL["run"], "id": run_a_id}}
    detail_b = {
        **_STUB_RUN_DETAIL,
        "run": {**_STUB_RUN_DETAIL["run"], "id": run_b_id, "branch": "feature-b"},
    }

    # Track every detail fetch so the test can assert on which run
    # ids + which views were requested.
    fetched: list[str] = []

    def _route_detail(detail_payload: dict[str, object], expected_id: int):
        def handler(route):
            fetched.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(detail_payload),
            )
        return handler

    page.route(f"**/api/e2e-run-detail/{run_a_id}**", _route_detail(detail_a, run_a_id))
    page.route(f"**/api/e2e-run-detail/{run_b_id}**", _route_detail(detail_b, run_b_id))

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    page.evaluate(
        f"""(payload) => {{
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {{
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }}
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }}""",
        _STUB_RECENT_RUNS_PAYLOAD,
    )

    row_a = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_a_id}']")
    row_b = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_b_id}']")
    expect(row_a).to_have_count(1)
    expect(row_b).to_have_count(1)

    # ── 1. Expand both rows ─────────────────────────────────────────
    row_a.locator("summary").first.click()
    expect(row_a.locator(".cvv-root")).to_be_visible(timeout=10_000)
    row_b.locator("summary").first.click()
    expect(row_b.locator(".cvv-root")).to_be_visible(timeout=10_000)

    # Each row got its OWN detail fetch.
    assert any(f"/api/e2e-run-detail/{run_a_id}" in url for url in fetched), \
        f"row A detail not fetched; saw: {fetched}"
    assert any(f"/api/e2e-run-detail/{run_b_id}" in url for url in fetched), \
        f"row B detail not fetched; saw: {fetched}"

    # ── 2. Two distinct Diagnostics disclosures ────────────────────
    # Each row has ONE — not the SSR-collision case of two
    # ``#runDetailsDisclosure`` ids in the DOM.
    expect(row_a.locator(".run-details-disclosure")).to_have_count(1)
    expect(row_b.locator(".run-details-disclosure")).to_have_count(1)
    # Open the disclosure inside each row.
    row_a.locator(".run-details-disclosure summary").first.click()
    row_b.locator(".run-details-disclosure summary").first.click()
    expect(row_a.locator(".e2e-timeline-content")).to_be_visible(timeout=5_000)
    expect(row_b.locator(".e2e-timeline-content")).to_be_visible(timeout=5_000)

    # ── 3. Each row's view-switcher Story/Ops/Debug button carries
    # a typed Command pinned to THAT row's run id ────────────────
    btn_a_ops = row_a.locator(".e2e-view-btn[data-view='ops']").first
    cmd_a = json.loads(
        (btn_a_ops.get_attribute("data-lifecycle-command") or "")
        .replace("&quot;", '"').replace("&amp;", "&")
    )
    assert cmd_a == {
        "kind": "switch_e2e_timeline_view",
        "label": "Switch diagnostics timeline to Ops",
        "run_id": run_a_id,
        "view": "ops",
    }, f"row A Ops button has wrong typed Command: {cmd_a!r}"

    btn_b_debug = row_b.locator(".e2e-view-btn[data-view='debug']").first
    cmd_b = json.loads(
        (btn_b_debug.get_attribute("data-lifecycle-command") or "")
        .replace("&quot;", '"').replace("&amp;", "&")
    )
    assert cmd_b["run_id"] == run_b_id, \
        f"row B Debug button must carry run_id={run_b_id}, got {cmd_b!r}"
    assert cmd_b["view"] == "debug"

    # ── 4. Click Row A's Ops button — only Row A's timeline refetches ─
    fetched.clear()
    with page.expect_response(
        lambda response: f"/api/e2e-run-detail/{run_a_id}" in response.url
        and "view=ops" in response.url,
        timeout=5_000,
    ):
        btn_a_ops.click()
    # The fetch went to row A's run id with view=ops.
    a_ops_fetches = [u for u in fetched if f"/api/e2e-run-detail/{run_a_id}" in u and "view=ops" in u]
    assert a_ops_fetches, f"row A Ops click did not fetch row A detail; saw: {fetched}"
    # Row B did NOT refetch.
    assert not any(f"/api/e2e-run-detail/{run_b_id}" in u for u in fetched), \
        f"row B timeline must NOT refetch when row A switches view; saw: {fetched}"

    # ── 5. The ``active`` class moved on row A's switcher only ──────
    expect(row_a.locator(".e2e-view-btn[data-view='ops']")).to_have_class(
        "e2e-view-btn active"
    )
    # Row B's Story remains active (untouched).
    expect(row_b.locator(".e2e-view-btn[data-view='user']")).to_have_class(
        "e2e-view-btn active"
    )

    # ── 6. The untracked-failures banners carry typed
    # ``create_e2e_untriaged_issues`` Commands pinned to each row's
    # run id (so a click in row A targets only row A's untriaged
    # tests). ─────────────────────────────────────────────────────
    banner_a_btn = row_a.locator(".e2e-untracked-banner button.btn-primary").first
    create_a = json.loads(
        (banner_a_btn.get_attribute("data-lifecycle-command") or "")
        .replace("&quot;", '"').replace("&amp;", "&")
    )
    assert create_a["kind"] == "create_e2e_untriaged_issues"
    assert create_a["run_id"] == run_a_id
    # Each row has its OWN agent select — no document-global ``#unifiedRunAgent``.
    expect(row_a.locator(".unified-run-agent")).to_have_count(1)
    expect(row_b.locator(".unified-run-agent")).to_have_count(1)
    expect(page.locator("#unifiedRunAgent")).to_have_count(0)

    assert not errors, f"unexpected page errors: {errors}"


def test_open_e2e_run_command_reroutes_to_row_expansion_not_modal(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Issue #6334: ``open_e2e_run`` dispatches to ``expandE2ERunRow``,
    which opens the matching row inline — the dropped
    ``showUnifiedRunView`` modal driver is no longer in the dispatcher.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.route(
        f"**/api/e2e-run-detail/{_RUN_ID}**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RUN_DETAIL),
        ),
    )

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    # Inject the mount point + render the runs list (see explanation
    # in the inline-mount test above).
    page.evaluate(
        f"""(payload) => {{
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {{
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }}
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }}""",
        _STUB_RECENT_RUNS_PAYLOAD,
    )

    # Dispatch the typed ``open_e2e_run`` Command directly via the
    # global dispatcher — same path the chip / View button take.
    page.evaluate(
        f"""() => window.runLifecycleCommand({{
            kind: 'open_e2e_run',
            label: 'Open E2E Run',
            run_id: {_RUN_ID},
            expand_run_details: false,
        }})"""
    )

    # The matching row opens (and scrolls into view).
    target_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID}']")
    expect(target_row).to_have_js_property("open", True)
    # Canonical viewer mounted via the same lazy-fetch path.
    expect(target_row.locator(".cvv-root")).to_be_visible(timeout=10_000)

    # No modal popped open — the dropped ``#e2eDiagnosisModal`` is
    # not in the DOM at all.
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    assert not errors, f"unexpected page errors: {errors}"


# ─── Gap 1: Create-Issues click-through in two-row mode ────────────────


def test_create_issues_for_untriaged_uses_row_scoped_agent_and_run_id(
    page: Page,
    two_agent_web_server: _TwoAgentWebServer,
) -> None:
    """End-to-end click-through proves the ``create_e2e_untriaged_issues``
    typed Command resolves the agent + nodeids + run_id from the
    *row the user clicked from*, not from a shared singleton.

    With two rows expanded, the user selects a DIFFERENT agent in
    each row, then clicks "Create issue(s)" inside row A.  The
    resulting POST must hit ``/control/e2e/create-issues/{row_a_id}``
    with ``agent=<row_a_agent>`` and the untriaged nodeids from
    row A's detail payload.  Row B is never touched.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    run_a_id = _RUN_ID
    run_b_id = _RUN_ID + 1
    # Each row's detail carries DIFFERENT untriaged test sets so we
    # can assert which row's nodeids landed in the POST body.
    detail_a = {
        **_STUB_RUN_DETAIL,
        "run": {**_STUB_RUN_DETAIL["run"], "id": run_a_id},
        "results_by_category": {
            **_STUB_RUN_DETAIL["results_by_category"],
            "untriaged": [
                {
                    "nodeid": "tests/row_a.py::test_alpha",
                    "outcome": "failed", "duration_seconds": 0.0,
                    "failure_summary": "", "longrepr": "", "history": [],
                    "existing_issue": None, "is_quarantined": False,
                    "result_source": "junit_xml", "suite_name": "tests/row_a.py",
                },
            ],
        },
    }
    detail_b = {
        **_STUB_RUN_DETAIL,
        "run": {**_STUB_RUN_DETAIL["run"], "id": run_b_id},
        "results_by_category": {
            **_STUB_RUN_DETAIL["results_by_category"],
            "untriaged": [
                {
                    "nodeid": "tests/row_b.py::test_bravo",
                    "outcome": "failed", "duration_seconds": 0.0,
                    "failure_summary": "", "longrepr": "", "history": [],
                    "existing_issue": None, "is_quarantined": False,
                    "result_source": "junit_xml", "suite_name": "tests/row_b.py",
                },
            ],
        },
    }

    page.route(
        f"**/api/e2e-run-detail/{run_a_id}**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(detail_a),
        ),
    )
    page.route(
        f"**/api/e2e-run-detail/{run_b_id}**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(detail_b),
        ),
    )

    # Intercept the bulk-create endpoint and capture every body the
    # frontend sent. ``page.expect_response`` waits until the route
    # handler has captured and fulfilled the POST.
    create_calls: list[_CapturedCreateIssuesCall] = []

    def _route_create(route: Route) -> None:
        post_data = route.request.post_data
        if post_data is None:
            raise ValueError("create-issues request must carry a JSON body")
        create_calls.append(
            _CapturedCreateIssuesCall(
                route.request.url,
                _CreateIssuesRequest.from_json(post_data),
            )
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "parent_issue": {"number": 9000, "url": "https://example/9000"},
                "sub_issues": [{"number": 9001}],
            }),
        )

    page.route(f"**/control/e2e/create-issues/{run_a_id}**", _route_create)
    page.route(f"**/control/e2e/create-issues/{run_b_id}**", _route_create)

    # Inject ``window.open`` so the post-success new-tab call is a
    # no-op (otherwise Playwright opens a real popup tab).
    page.add_init_script("window.open = () => null;")

    _goto_dashboard_e2e_tab(page, two_agent_web_server.url)
    # Both the initial page and every background view-model refresh now carry
    # the same two-agent server truth. The earlier browser-only override raced
    # an ordinary refresh and could remove ``agent:vscode`` between the two
    # asynchronous detail renders.
    page.evaluate(
        """(payload) => {
            window.REPO_ROOT = window.REPO_ROOT || '/tmp/repo';
            window.CONFIG_NAME = window.CONFIG_NAME || 'default.yaml';
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }""",
        _STUB_RECENT_RUNS_PAYLOAD,
    )
    # Force the previously racy refresh boundary before either async detail
    # render. The server must republish the same agents, not erase the second
    # option as the old browser-only override did.
    page.evaluate("() => refreshViewModel({reloadOnListChange: false})")
    assert page.evaluate("() => window.dashboardData.agents") == [
        "agent:web",
        "agent:vscode",
    ]

    row_a = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_a_id}']")
    row_b = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_b_id}']")

    # Expand both rows so each mounts its own canonical viewer +
    # untracked-failures banner.
    row_a.locator("summary").first.click()
    expect(row_a.locator(".cvv-root")).to_be_visible(timeout=10_000)
    row_b.locator("summary").first.click()
    expect(row_b.locator(".cvv-root")).to_be_visible(timeout=10_000)

    # Each row has its OWN agent select (proves the legacy
    # document-global ``#unifiedRunAgent`` id is gone).
    expect(row_a.locator(".unified-run-agent")).to_have_count(1)
    expect(row_b.locator(".unified-run-agent")).to_have_count(1)
    expect(page.locator("#unifiedRunAgent")).to_have_count(0)

    # Pick a DIFFERENT agent in each row — if the handler resolves
    # the agent globally we'll see the wrong value in the POST.
    row_a.locator(".unified-run-agent").select_option("agent:web")
    row_b.locator(".unified-run-agent").select_option("agent:vscode")

    # Click row A's Create-issues button + wait for the POST to fire.
    create_btn_a = row_a.locator(".e2e-untracked-banner button.btn-primary").first
    with page.expect_response(
        lambda response: f"/control/e2e/create-issues/{run_a_id}" in response.url
        and response.request.method == "POST",
        timeout=5_000,
    ):
        create_btn_a.click()

    # Exactly one POST fired, against row A's run id, with row A's
    # agent + row A's untriaged nodeids.  Row B's URL never hit.
    a_calls = [
        call for call in create_calls if f"/create-issues/{run_a_id}" in call.url
    ]
    b_calls = [
        call for call in create_calls if f"/create-issues/{run_b_id}" in call.url
    ]
    assert len(a_calls) == 1, f"expected 1 POST to row A's URL, got: {create_calls}"
    assert not b_calls, f"row B must not be touched; saw: {create_calls}"

    a_request = a_calls[0].request
    assert a_request.agent == "agent:web", (
        f"create-issues POST carried wrong agent — handler resolved against a "
        f"different row's select? got: {a_request!r}"
    )
    assert a_request.nodeids == ("tests/row_a.py::test_alpha",), (
        f"create-issues POST carried wrong nodeids — handler resolved against a "
        f"different row's detail? got: {a_request!r}"
    )

    assert not errors, f"unexpected page errors: {errors}"


# ─── Gap 2: SSR mounting path (template embeds JSON, chunk reads it) ──


import socket
import time
from pathlib import Path
from threading import Thread

import pytest
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
import uvicorn

import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UvicornTestServer:
    def __init__(self, host: str, port: int) -> None:
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
        self.server = uvicorn.Server(config)
        self.thread = Thread(target=self.server.run, daemon=True)
        self.host = host
        self.port = port

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.socket()
                s.connect((self.host, self.port))
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("uvicorn server did not come up in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.server.force_exit = True
        PROCESS_COMPLETION_WATCHDOG.join_thread(
            self.thread,
            operation="inline E2E runs server shutdown",
        )


@pytest.fixture
def e2e_enabled_web_server(tmp_path: Path):
    """A web server whose mock orchestrator has E2E enabled + an e2e.db
    seeded with one run, so the SSR template renders the runs-list
    mount points without manual JS injection.

    Used by ``test_ssr_path_renders_runs_from_inline_payload`` to
    cover the production wiring template → ``#recentE2ERunsData``
    inline JSON → ``e2e_runs_list.js`` ``DOMContentLoaded`` →
    ``renderE2ERunsList``.
    """
    from issue_orchestrator.infra.e2e_db import E2EDB

    orchestrator = MockOrchestratorForWeb()
    repo_root = tmp_path / "repo"
    (repo_root / ".issue-orchestrator").mkdir(parents=True)
    orchestrator.config.repo_root = repo_root
    orchestrator.config.config_path = repo_root / ".issue-orchestrator" / "default.yaml"
    orchestrator.config.e2e.enabled = True

    # Seed one E2E run in the DB so ``build_recent_e2e_runs``
    # returns a non-empty payload.
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    db = E2EDB(db_path)
    run_id = db.start_run(
        orchestrator_id=orchestrator.config.orchestrator_id,
        repo_root=str(repo_root),
        commit_sha="abc1234",
        branch="main",
        pytest_args=[],
        command=["pytest", "tests/e2e"],
        runner_kind="pytest",
    )
    db.finish_run(run_id, status="passed", exit_code=0)

    port = _find_free_port()
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)
    server = _UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "run_id": run_id,
            "orchestrator": orchestrator,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)


def test_ssr_path_renders_runs_from_inline_payload(
    page: Page,
    e2e_enabled_web_server: dict[str, object],
) -> None:
    """The production wiring works end-to-end: the dashboard template
    embeds the typed ``RecentE2ERunsPayload`` as inline JSON
    (``#recentE2ERunsData``), and ``e2e_runs_list.js`` reads it on
    ``DOMContentLoaded`` and renders rows into ``#e2eRunsListRoot``.

    This closes the gap where the other Playwright smokes had to
    inject the runs list via ``page.evaluate`` because the default
    fixture has E2E disabled.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    run_id = e2e_enabled_web_server["run_id"]
    base_url = str(e2e_enabled_web_server["url"])
    page.goto(f"{base_url}/?tab=e2e", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)

    # The chunk reads the inline JSON on DOMContentLoaded; the row
    # for the seeded run id appears WITHOUT any test-side JS injection.
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5_000)

    # The typed Command on the row matches what the Pydantic model
    # would emit — proves the template's ``| tojson`` pipeline
    # produced a valid wire shape the JS could decode.
    cmd_raw = row.get_attribute("data-lifecycle-command") or ""
    cmd = json.loads(cmd_raw.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd == {
        "kind": "expand_e2e_run",
        "label": "Expand E2E Run",
        "run_id": int(run_id),
    }

    # Modal is gone in this surface too (regression guard for the
    # SSR path).
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    assert not errors, f"unexpected page errors: {errors}"


# ─── UI parity: the canonical viewer body is identical across surfaces ─


def test_canonical_viewer_body_identical_across_run_view_and_validation_modal(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """An end-user sees the SAME canonical viewer content regardless of
    which dashboard surface mounted it.

    The dashboard's inline E2E row body calls
    ``renderE2EResultsPanel(data)``, which internally runs
    ``e2eRunToCanonicalPayload(data)`` and then
    ``renderCanonicalValidationViewer(canonical)``.  The validation
    modal / issue-detail drawer call ``renderCanonicalValidationViewer``
    directly with the canonical payload.

    This test honestly exercises BOTH call paths in the live
    browser:

      1. Build the E2E results-by-category ``data`` shape the
         dashboard uses.
      2. Render via ``renderE2EResultsPanel(data)``, then extract
         the ``.cvv-root`` subtree.
      3. Compute the canonical payload the dashboard would derive
         (``e2eRunToCanonicalPayload(data)``).
      4. Render via ``renderCanonicalValidationViewer(canonical)``
         directly — same call the validation modal / drawer make.
      5. Assert the two ``.cvv-root`` HTML strings are byte-identical.

    A regression where the dashboard's translator drifts away from
    what the modal/drawer expect — or where the run-view path adds
    a per-test class the modal path doesn't — would fire this test.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))

    # E2E results-by-category shape (one untracked failure, one
    # linked failure, two passes, one skipped) — every translator
    # branch is exercised.
    e2e_run_detail = {
        "run": {
            "id": 9001,
            "status": "failed",
            "started_at": "2026-05-12T01:00:00Z",
            "duration_seconds": 0.5,
            "commit_sha": "abc",
            "branch": "main",
            "runner_kind": "pytest",
            "command": ["pytest"],
        },
        "results_by_category": {
            "untriaged": [{
                "nodeid": "tests/a.py::test_untracked",
                "suite_name": "tests/a.py",
                "outcome": "failed",
                "duration_seconds": 0.1,
                "failure_summary": "AssertionError: untracked",
                "longrepr": "AssertionError: untracked\n  at line 1",
                "history": [],
                "existing_issue": None,
                "is_quarantined": False,
                "result_source": "junit_xml",
            }],
            "has_issue": [{
                "nodeid": "tests/b.py::test_linked",
                "suite_name": "tests/b.py",
                "outcome": "failed",
                "duration_seconds": 0.2,
                "failure_summary": "AssertionError: linked",
                "longrepr": "AssertionError: linked\n  at line 2",
                "history": [],
                "existing_issue": {"number": 1234, "title": "x", "state": "open"},
                "is_quarantined": False,
                "result_source": "junit_xml",
            }],
            "passed": [
                {"nodeid": "tests/c.py::test_p1", "suite_name": "tests/c.py",
                 "outcome": "passed", "duration_seconds": 0.01},
                {"nodeid": "tests/c.py::test_p2", "suite_name": "tests/c.py",
                 "outcome": "passed", "duration_seconds": 0.01},
            ],
            "flaky": [],
            "fixed": [],
            "quarantined": [],
            "skipped": [{
                "nodeid": "tests/d.py::test_skip",
                "suite_name": "tests/d.py",
                "outcome": "skipped",
                "duration_seconds": 0.0,
                "failure_details": "skip(reason='pending'): pending",
            }],
        },
        "results_summary": {"total": 5, "passed": 2, "failed": 2, "skipped": 1,
                            "untriaged": 1, "has_issue": 1, "flaky": 0, "fixed": 0,
                            "quarantined": 0},
        "artifacts": [], "reports": [], "issue_affordances": [],
        "lifecycle": None, "events": [], "phase_toc": [], "cycles": [],
    }

    # Real cross-surface comparison: each call path mounts its own
    # ``.cvv-root`` from the live module code, no string-wrapping.
    compare = page.evaluate(
        """(data) => {
            // ── Path A: dashboard E2E run-view ───────────────────
            // ``renderE2EResultsPanel`` runs ``e2eRunToCanonicalPayload``
            // internally and then calls ``renderCanonicalValidationViewer``.
            const panelHtml = window.renderE2EResultsPanel(data);
            const panelHost = document.createElement('div');
            panelHost.innerHTML = panelHtml;
            const cvvFromPanel = panelHost.querySelector('.cvv-root');

            // ── Path B: validation modal / issue-detail drawer ───
            // These surfaces call ``renderCanonicalValidationViewer``
            // directly with a canonical payload.  Compute the
            // canonical payload the same way the dashboard does
            // (the translator is exposed as a top-level symbol so
            // both surfaces share it).
            const canonical = window.e2eRunToCanonicalPayload(data);
            const directHtml = window.renderCanonicalValidationViewer(canonical);
            const directHost = document.createElement('div');
            directHost.innerHTML = directHtml;
            const cvvFromDirect = directHost.querySelector('.cvv-root');

            return {
                fromPanel: cvvFromPanel ? cvvFromPanel.outerHTML : null,
                fromDirect: cvvFromDirect ? cvvFromDirect.outerHTML : null,
                // Also surface the translator output so a mismatch
                // diff can show which canonical input each path saw.
                canonicalKind: typeof canonical,
                canonicalCases: Array.isArray(canonical && canonical.junit_cases)
                    ? canonical.junit_cases.length : -1,
            };
        }""",
        e2e_run_detail,
    )
    assert compare["fromPanel"] is not None, (
        "dashboard run-view path failed to produce .cvv-root from renderE2EResultsPanel"
    )
    assert compare["fromDirect"] is not None, (
        "validation-modal/drawer path failed to produce .cvv-root from renderCanonicalValidationViewer"
    )
    assert compare["canonicalCases"] >= 5, (
        f"translator under-produced cases: {compare['canonicalCases']}"
    )
    # The single byte-equality assertion that the parity contract
    # demands: same canonical input → identical .cvv-root HTML.
    if compare["fromPanel"] != compare["fromDirect"]:
        # Surface a useful diff prefix.
        a = compare["fromPanel"]
        b = compare["fromDirect"]
        for i, (ca, cb) in enumerate(zip(a, b)):
            if ca != cb:
                start = max(0, i - 60)
                end = min(min(len(a), len(b)), i + 60)
                raise AssertionError(
                    f"canonical .cvv-root diverged at char {i}:\n"
                    f"  panel:  …{a[start:end]!r}…\n"
                    f"  direct: …{b[start:end]!r}…"
                )
        raise AssertionError(
            f"canonical .cvv-root lengths differ — panel={len(a)} vs direct={len(b)}"
        )

    assert not errors, f"unexpected page errors: {errors}"
