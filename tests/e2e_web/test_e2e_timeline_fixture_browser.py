"""Browser-driven test for the E2E run drawer's issue affordances.

This is the highest-fidelity check we have that the dashboard's E2E
timeline actually renders clickable issue links: it stages a captured
real-data fixture into a tmp_path, points a real uvicorn server at it,
loads the dashboard in Playwright, opens the run drawer, switches to
the Timeline tab through a real UI affordance, and asserts that the
rendered HTML contains issue controls that resolve to the dashboard
issue detail drawer.

It catches a class of bug the JSON-only integration test cannot:
- frontend regressions in ``renderTimeline`` that drop ``issue_numbers``
- CSS that hides ``.timeline-issue-links``
- broken ``openIssueDetail`` plumbing
- contract drift between ``/control/e2e/run/{id}/timeline`` and the JS
  that consumes its response

The fixture is the same one used by
``tests/integration/test_e2e_timeline_real_fixture.py``, so a
single ``snapshot_e2e_run.py`` capture covers both layers.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import socket
import time
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlencode

import pytest
import uvicorn
from playwright.sync_api import Locator, Page, expect

from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from issue_orchestrator.view_models.lifecycle_semantics import (
    DashboardTimelineContainer,
    E2ESuiteTimelineContainer,
)
from tests.fixtures.timeline_run_artifacts import (
    TimelineFixturePathField,
    rewrite_timeline_fixture_path,
    write_available_timeline_run_manifest,
)
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "e2e_runs"
RUN_FIXTURE = FIXTURE_DIR / "run_88"
RUN_FIXTURE_ID = 88

# Issue used for the session-log click-through in run_88. This issue
# appears in both test_inflight_refresh_discovers_issue and test_4057
# rows (per expected.json), so whichever row the test clicks, the
# synthetic session recording is reachable. Its compact branch label
# renders as "inflight-discovery".
TEST_CLICK_ISSUE_NUMBER = 5723
TEST_CLICK_ISSUE_LABEL = "inflight-discovery"
# Second issue on the test_4057 row — used to verify multi-affordance
# row rendering with distinct labels.
TEST_CLICK_ISSUE_NUMBER_2 = 5724
TEST_CLICK_ISSUE_LABEL_2 = "ui-surface-provider-cir\u2026"
_INVALID_TIME_TEXTS = {"", "-", "n/a", "na", "unknown"}
FIXTURE_REVIEW_PHASE_RECORDING_ROLES = {
    "review_exchange.round_started": "reviewer",
    "review_exchange.round_completed": "reviewer",
    "review.rework_started": "coder",
    "review.rework_completed": "coder",
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_MINIMAL_CONFIG_YAML = """
worktrees:
  base: "../"

agents:
  "agent:test":
    prompt: "test.md"

e2e:
  enabled: true
  role: "reader"
  auto_run_interval_minutes: 0
  pytest_args: ["tests/e2e"]
"""

# Recognizable text written into the synthetic terminal recording so the
# Playwright test can prove the session-log endpoint returned real content.
_SYNTHETIC_SESSION_OUTPUT = "PLAYWRIGHT-FIXTURE-SESSION-STARTED"
_SYNTHETIC_SESSION_OUTPUT_FOLLOWUP = "PLAYWRIGHT-FIXTURE-AGENT-READY"
_SYNTHETIC_RUN_LOG_TEXT = "PLAYWRIGHT-FIXTURE-RUN-LOG"


def _materialize_synthetic_session_dir(
    session_dir: Path,
    *,
    issue_number: int,
) -> None:
    """Create a real run_dir with a non-empty terminal-recording.jsonl.

    The session-log action decorator (``_preferred_run_scoped_session_action``
    in web.py) refuses to emit an ``open_agent_log`` affordance unless the
    event's ``run_dir`` actually exists on disk. The session-replay endpoint
    additionally requires ``terminal-recording.jsonl`` to be non-empty. This
    helper synthesizes the minimum layout that both checks accept.

    The file contains:
      - one ``resize`` event (provides initial_geometry)
      - two ``output`` events whose base64 text is chosen to be searchable
        from the Playwright test via ``sessionReplayState.events``.
    """
    import base64

    session_dir.mkdir(parents=True, exist_ok=True)
    recording = session_dir / "terminal-recording.jsonl"
    events = [
        {
            "event_type": "resize",
            "offset_ms": 0,
            "rows": 24,
            "cols": 80,
        },
        {
            "event_type": "output",
            "offset_ms": 100,
            "data_b64": base64.b64encode(
                (_SYNTHETIC_SESSION_OUTPUT + "\r\n").encode("utf-8"),
            ).decode("ascii"),
        },
        {
            "event_type": "output",
            "offset_ms": 250,
            "data_b64": base64.b64encode(
                (_SYNTHETIC_SESSION_OUTPUT_FOLLOWUP + "\r\n").encode("utf-8"),
            ).decode("ascii"),
        },
    ]
    recording.write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n",
    )
    write_available_timeline_run_manifest(
        run_dir=session_dir,
        terminal_recording=recording,
        issue_number=issue_number,
    )


def _write_review_phase_terminal_recording(
    run_dir: Path,
    *,
    round_index: int,
    role: str,
) -> None:
    import base64

    recording = (
        run_dir
        / "review-exchange"
        / f"round-{round_index:03d}"
        / role
        / "terminal-recording.jsonl"
    )
    recording.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.b64encode(
        f"browser fixture {role} round {round_index}\n".encode(),
    ).decode("ascii")
    events = [
        {
            "event_type": "resize",
            "offset_ms": 0,
            "rows": 30,
            "cols": 120,
        },
        {
            "event_type": "output",
            "offset_ms": 1,
            "data_b64": payload,
        },
    ]
    recording.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def _wire_event_to_session_dir(
    worktree_db: Path,
    issue_number: int,
    event_name: str,
    session_dir: Path,
) -> None:
    """Point one worktree-timeline event at a real, on-disk run_dir.

    Updates both the ``run_dir`` column and the ``data_json.run_dir`` field
    of a single matching row so the action decorator's path-exists check
    succeeds and a ``Coding Recording`` button appears in the issue-detail
    drawer for that event.

    Raises if the target event is not found — silent misses would leave
    the Playwright test asserting against an empty state.
    """
    import sqlite3

    run_dir_str = str(session_dir)
    with sqlite3.connect(str(worktree_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT sequence, source_event, data_json FROM timeline_events
            WHERE issue_number = ? AND event = ? AND run_dir != ''
            ORDER BY sequence ASC LIMIT 1
            """,
            (issue_number, event_name),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"Cannot wire session dir: no {event_name} event with a "
                f"non-empty run_dir found for issue {issue_number}"
            )
        data = json.loads(row["data_json"]) if row["data_json"] else {}
        if isinstance(data, dict):
            data["run_dir"] = run_dir_str
            canonical_event_name = str(row["source_event"] or event_name)
            for field in TimelineFixturePathField:
                value = data.get(field.value)
                if not isinstance(value, str):
                    continue
                data[field.value] = str(
                    rewrite_timeline_fixture_path(
                        field=field,
                        run_dir=session_dir,
                        original_value=value,
                        event_name=canonical_event_name,
                        event_data=data,
                    )
                )
        conn.execute(
            "UPDATE timeline_events SET run_dir = ?, data_json = ? WHERE sequence = ?",
            (run_dir_str, json.dumps(data), row["sequence"]),
        )
        conn.commit()


def _materialize_fixture_run_dirs(worktree_db: Path, run_dir_root: Path) -> None:
    """Replace sanitized fixture run_dir paths with real directories."""
    import sqlite3

    rows: list[tuple[int, int, str, str, str, str]] = []
    with sqlite3.connect(str(worktree_db)) as conn:
        for (
            sequence,
            issue_number,
            event_name,
            source_event,
            run_dir,
            data_json,
        ) in conn.execute(
            """
            SELECT sequence, issue_number, event, source_event, run_dir, data_json
            FROM timeline_events
            WHERE run_dir != ''
            ORDER BY sequence ASC
            """
        ):
            rows.append(
                (
                    int(sequence),
                    int(issue_number),
                    str(event_name),
                    str(source_event),
                    str(run_dir),
                    str(data_json or "{}"),
                )
            )

        replacements: dict[str, tuple[Path, int]] = {}
        for (
            _sequence,
            issue_number,
            _event_name,
            _source_event,
            original_run_dir,
            _data_json,
        ) in rows:
            if original_run_dir not in replacements:
                run_index = len(replacements) + 1
                synthetic_run_dir = (
                    run_dir_root
                    / f"worktree-{run_index}"
                    / ".issue-orchestrator"
                    / "sessions"
                    / f"fixture-{run_index}__session-{run_index}"
                )
                _materialize_synthetic_session_dir(
                    synthetic_run_dir,
                    issue_number=issue_number,
                )
                replacements[original_run_dir] = (synthetic_run_dir, issue_number)
            elif replacements[original_run_dir][1] != issue_number:
                raise ValueError(
                    "Captured Timeline run belongs to multiple issues: "
                    f"run_dir={original_run_dir}"
                )

        for (
            sequence,
            _issue_number,
            event_name,
            source_event,
            original_run_dir,
            data_json,
        ) in rows:
            synthetic_run_dir = replacements[original_run_dir][0]
            data = json.loads(data_json)
            if isinstance(data, dict):
                data["run_dir"] = str(synthetic_run_dir)
                canonical_event_name = source_event or event_name
                role = FIXTURE_REVIEW_PHASE_RECORDING_ROLES.get(event_name)
                round_index = data.get("round_index")
                if role is not None and isinstance(round_index, int):
                    _write_review_phase_terminal_recording(
                        synthetic_run_dir,
                        round_index=round_index,
                        role=role,
                    )
                for field in TimelineFixturePathField:
                    value = data.get(field.value)
                    if not isinstance(value, str):
                        continue
                    data[field.value] = str(
                        rewrite_timeline_fixture_path(
                            field=field,
                            run_dir=synthetic_run_dir,
                            original_value=value,
                            event_name=canonical_event_name,
                            event_data=data,
                        )
                    )
            conn.execute(
                "UPDATE timeline_events SET run_dir = ?, data_json = ? WHERE sequence = ?",
                (str(synthetic_run_dir), json.dumps(data), sequence),
            )
        conn.commit()


def _stage_fixture(fixture: Path, tmp_path: Path) -> Path:
    """Mirror the integration-test fixture stager so the live endpoint
    code path runs unmodified against on-disk data.

    In addition to the base fixture copy, this stager materializes ONE
    real session run_dir at ``tmp_path/session1`` containing a
    synthetic ``terminal-recording.jsonl``, and rewires the
    ``TEST_CLICK_ISSUE_NUMBER`` event to reference it. This lets the
    Playwright test verify the full click-through from timeline row →
    issue detail drawer → session recording modal → terminal content.
    Without this step the fixture has no session logs to view.

    Also writes a minimal default.yaml so the run-details endpoint
    (which the dashboard fetches in parallel with the timeline
    endpoint when opening the run drawer) can resolve a config without
    crashing.
    """
    repo_root = tmp_path / "repo"
    state_dir = repo_root / ".issue-orchestrator" / "state"
    config_dir = repo_root / ".issue-orchestrator" / "config"
    log_dir = repo_root / ".issue-orchestrator" / "logs"
    state_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
    wt_state.mkdir(parents=True)

    shutil.copy(fixture / "e2e.db", repo_root / ".issue-orchestrator" / "e2e.db")
    shutil.copy(fixture / "base_timeline.sqlite", state_dir / "timeline.sqlite")
    shutil.copy(fixture / "worktree_timeline.sqlite", wt_state / "timeline.sqlite")
    (config_dir / "default.yaml").write_text(_MINIMAL_CONFIG_YAML)

    import sqlite3

    run_log_path = log_dir / f"run-{RUN_FIXTURE_ID}.log"
    run_log_path.write_text(f"{_SYNTHETIC_RUN_LOG_TEXT}\n", encoding="utf-8")

    with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as conn:
        conn.execute(
            "UPDATE e2e_runs SET orchestrator_id = ?, log_path = ? WHERE id = ?",
            (repo_root.name, str(run_log_path), RUN_FIXTURE_ID),
        )
        conn.commit()

    # Materialize real session run_dirs and wire the target issue's
    # run-scoped start events to point at them. The run_dir must contain
    # a non-empty terminal-recording.jsonl for both the action decorator
    # and the session-replay endpoint to surface real data.
    _materialize_fixture_run_dirs(wt_state / "timeline.sqlite", tmp_path / "run-dirs")
    session_dir = (
        tmp_path
        / "session1"
        / ".issue-orchestrator"
        / "sessions"
        / "fixture-coding__session1"
    )
    review_session_dir = (
        tmp_path
        / "review-session1"
        / ".issue-orchestrator"
        / "sessions"
        / "fixture-review__review-session1"
    )
    _materialize_synthetic_session_dir(
        session_dir,
        issue_number=TEST_CLICK_ISSUE_NUMBER,
    )
    _materialize_synthetic_session_dir(
        review_session_dir,
        issue_number=TEST_CLICK_ISSUE_NUMBER,
    )
    _wire_event_to_session_dir(
        wt_state / "timeline.sqlite",
        issue_number=TEST_CLICK_ISSUE_NUMBER,
        event_name="agent.coding_started",
        session_dir=session_dir,
    )
    _wire_event_to_session_dir(
        wt_state / "timeline.sqlite",
        issue_number=TEST_CLICK_ISSUE_NUMBER,
        event_name="review.started",
        session_dir=review_session_dir,
    )
    return repo_root


class _UvicornTestServer:
    def __init__(self, host: str, port: int) -> None:
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread: Thread | None = None

    def start(self) -> None:
        self.thread = Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.1)
                if sock.connect_ex((self.config.host, int(self.config.port))) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(
            f"Test server failed to start on {self.config.host}:{self.config.port}"
        )

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture
def fixture_web_server(tmp_path: Path) -> dict[str, object]:
    """Stage run_87 into tmp_path and start a real uvicorn server pointing at it.

    The control endpoints (``/control/e2e/run/{id}/timeline`` etc.) read
    timeline data straight from disk based on the ``repo_root`` query
    param, so the orchestrator only needs to expose ``config.repo_root``
    pointing at the staged layout. ``deps.timeline_store`` is wired to a
    real SqliteTimelineStore for completeness.
    """
    if not RUN_FIXTURE.exists():
        pytest.skip(
            f"{RUN_FIXTURE.name} fixture missing at {RUN_FIXTURE}. "
            f"Capture with: scripts/snapshot_e2e_run.py --run-id {RUN_FIXTURE_ID}"
        )

    repo_root = _stage_fixture(RUN_FIXTURE, tmp_path)
    base_store = SqliteTimelineStore(
        db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )

    orchestrator = MockOrchestratorForWeb()
    orchestrator.config.repo_root = repo_root
    # The dashboard view model uses config.config_path.name as the JS
    # CONFIG_NAME global; the run-details endpoint then passes it to
    # _load_config_by_name. Point at the staged config so the lookup
    # resolves to a real file.
    orchestrator.config.config_path = (
        repo_root / ".issue-orchestrator" / "config" / "default.yaml"
    )
    orchestrator.config.e2e.enabled = True

    # Build a realistic deps object: real timeline_store + reader for
    # the endpoints that read them, and MagicMock for the optional
    # accessories the issue-detail action-applier code touches
    # (publish_recovery, etc.). Using MagicMock as the base lets any
    # incidental attribute access return a sensible default instead
    # of crashing with AttributeError.
    from unittest.mock import MagicMock
    from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader

    deps = MagicMock()
    deps.timeline_store = base_store
    deps.timeline_reader = DefaultTimelineReader(base_store)
    # publish_recovery.can_retry_publish is consulted by the issue-detail
    # action applier — return False so the "retry publish" affordance
    # stays hidden (it's orthogonal to this test's contract).
    deps.publish_recovery.can_retry_publish.return_value = False
    orchestrator.deps = deps

    port = _find_free_port()
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)

    server = _UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "repo_root": repo_root,
            "run_id": RUN_FIXTURE_ID,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)


_TEST_4057_NODEID = (
    "tests/e2e/test_issue_4057_production_flow.py"
    "::test_4057_production_real_agents_publish_gate_and_diagnostics"
)
_TEST_INFLIGHT_NODEID = (
    "tests/e2e/test_inflight_refresh_discovers_issue.py"
    "::test_inflight_refresh_discovers_issue"
)


def _expect_parseable_time_text(page: Page, locator: Locator, description: str) -> str:
    """Assert a rendered browser timestamp is visible and browser-parseable."""
    expect(locator).to_be_visible(timeout=5000)
    text = (locator.text_content() or "").strip()
    assert text.lower() not in _INVALID_TIME_TEXTS, (
        f"{description} should render a concrete timestamp, got {text!r}"
    )
    parsed_epoch = page.evaluate(
        "(value) => Number.isNaN(new Date(value).getTime()) ? null : new Date(value).getTime()",
        text,
    )
    assert parsed_epoch is not None, (
        f"{description} should render browser-parseable timestamp text, got {text!r}"
    )
    return text


def _expect_all_parseable_time_texts(
    page: Page, locator: Locator, description: str
) -> list[str]:
    """Assert every matching timestamp element is visible and parseable."""
    expect(locator.first).to_be_visible(timeout=5000)
    items = locator.all()
    assert items, f"{description} should render at least one timestamp"
    return [
        _expect_parseable_time_text(page, item, f"{description} #{index}")
        for index, item in enumerate(items, start=1)
    ]


def _dom_click_hit_tested(locator: Locator, description: str) -> None:
    """Click via the DOM after proving the rendered element is hit-testable."""
    expect(locator).to_be_visible(timeout=5000)
    expect(locator).to_be_enabled(timeout=5000)
    locator.scroll_into_view_if_needed()
    hit_result = locator.evaluate(
        """
        (element) => {
            const rect = element.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(cx, cy);
            return {
                tag: hit ? hit.tagName : null,
                id: hit ? hit.id : null,
                className: hit && hit.className ? String(hit.className) : "",
                contains: !!hit && (hit === element || element.contains(hit)),
                pointerEvents: window.getComputedStyle(element).pointerEvents,
                disabled: !!element.disabled,
                width: rect.width,
                height: rect.height,
            };
        }
        """
    )
    assert hit_result["contains"], (
        f"{description} is not hit-testable at its center: {hit_result!r}"
    )
    assert hit_result["pointerEvents"] != "none", (
        f"{description} cannot receive pointer events: {hit_result!r}"
    )
    assert not hit_result["disabled"], f"{description} is disabled: {hit_result!r}"
    locator.evaluate("(element) => element.click()")


def _open_e2e_tab(page: Page) -> None:
    """Switch to the E2E tab and wait until the tab is visibly booted."""
    page.locator("#tab-e2e").click()
    page.wait_for_url("**?tab=e2e**")
    page.wait_for_function("() => window.dashboardBundleLoaded === true")
    page.wait_for_function("() => !document.documentElement.hasAttribute('data-booting')")
    expect(page.locator("#panel-e2e")).to_be_visible(timeout=5000)


def _browser_fetch_json(page: Page, url: str) -> dict[str, Any]:
    """Fetch JSON through the browser so assertions cover the page's contract surface."""
    result: dict[str, Any] | None = None
    for attempt in range(3):
        result = page.evaluate(
            """
            async (url) => {
                let response = null;
                try {
                    response = await fetch(url, { cache: "no-store" });
                } catch (err) {
                    return {
                        ok: false,
                        status: 0,
                        fetchError: String(err),
                    };
                }
                const text = await response.text();
                let payload = null;
                try {
                    payload = JSON.parse(text);
                } catch (err) {
                    return {
                        ok: response.ok,
                        status: response.status,
                        parseError: String(err),
                        text,
                    };
                }
                return { ok: response.ok, status: response.status, payload };
            }
            """,
            url,
        )
        assert isinstance(result, dict), f"browser fetch returned {result!r}"
        if "fetchError" not in result or attempt == 2:
            break
        page.evaluate(
            """
            ({ url, attempt, error }) => {
                console.warn(
                    `Retrying browser fetch ${attempt}/3 for ${url}: ${error}`
                );
            }
            """,
            {
                "url": url,
                "attempt": attempt + 1,
                "error": result["fetchError"],
            },
        )
        page.wait_for_timeout(250)

    assert result is not None
    assert "fetchError" not in result, (
        f"browser fetch failed for {url}: {result['fetchError']}"
    )
    assert result["ok"], (
        f"browser fetch failed for {url}: status={result['status']} "
        f"body={result.get('text') or result.get('payload')!r}"
    )
    assert "parseError" not in result, (
        f"browser fetch returned non-JSON for {url}: {result['parseError']} "
        f"body={result.get('text')!r}"
    )
    payload = result["payload"]
    assert isinstance(payload, dict), (
        f"browser fetch returned non-object JSON: {payload!r}"
    )
    return payload


def _url(base_url: str, path: str, **params: object) -> str:
    return f"{base_url}{path}?{urlencode(params)}"


def _user_e2e_run_detail_url(base_url: str, run_id: int | object) -> str:
    """Return the user-facing run-detail variant consumed by the dashboard modal."""
    return _url(str(base_url), f"/api/e2e-run-detail/{run_id}", view="user")


_PASSED_RUN_NODEID = "tixmeup.e2e.smoke::package.build_image"


def _synthetic_passed_run_payload(
    page: Page,
    base_url: str,
    run_id: int | object,
) -> dict[str, Any]:
    real_payload = _browser_fetch_json(
        page,
        _user_e2e_run_detail_url(base_url, run_id),
    )
    synthetic_payload = json.loads(json.dumps(real_payload))
    synthetic_payload["run"]["status"] = "passed"
    synthetic_payload["results_by_category"] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [
            {
                "nodeid": _PASSED_RUN_NODEID,
                "case_id": _PASSED_RUN_NODEID,
                "label": "package.build_image",
                "display_name": "package.build_image",
                "suite_name": "tixmeup.e2e.smoke",
                "outcome": "passed",
                "retry_outcome": None,
                "duration_seconds": 450.0,
                "longrepr": None,
                "history": [],
                "existing_issue": None,
                "flip_rate_percent": 0,
                "category": "healthy",
                "result_category": "passed",
                "result_source": "junit_xml",
                "is_quarantined": False,
            }
        ],
        "quarantined": [],
        "skipped": [],
    }
    synthetic_payload["results_summary"] = {
        "total": 1,
        "passed": 1,
        "untriaged": 0,
        "has_issue": 0,
        "flaky": 0,
        "fixed": 0,
        "quarantined": 0,
        "skipped": 0,
    }
    return synthetic_payload


def _route_synthetic_passed_run_detail(
    page: Page,
    base_url: str,
    run_id: int | object,
) -> None:
    synthetic_payload = _synthetic_passed_run_payload(page, base_url, run_id)
    page.route(
        _user_e2e_run_detail_url(base_url, run_id),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(synthetic_payload),
        ),
    )


def _expect_formatted_passed_run_modal(page: Page, run_id: int) -> None:
    # Issue #6334: the E2E run view mounts inline inside its row in
    # the runs-as-rows list — no more ``#e2eDiagnosisModal``.  This
    # helper asserts the same canonical-viewer contents the legacy
    # modal would have shown, just scoped to the row body.
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5000)
    expect(row).to_have_js_property("open", True)
    expect(row.locator(".diagnosis-header")).to_have_count(0)
    cvv = row.locator(".cvv-root").first
    expect(cvv).to_be_visible(timeout=5000)
    expect(cvv).to_have_attribute("data-cvv-status", "passed")
    # Run-level chips show "passed" status + 1 passing.
    summary = row.locator(".e2e-run-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("passed")
    expect(summary).to_contain_text("1 passing")
    # The passing test name appears in the rendered HTML (inside the
    # browse-by-file collapsible — it's in the DOM whether the row is
    # open or closed).
    expect(cvv).to_contain_text("package.build_image")


def _issue_affordance_numbers(timeline_payload: dict[str, Any]) -> list[int]:
    issue_numbers: list[int] = []
    for affordance in timeline_payload.get("issue_affordances", []):
        issue_number = affordance.get("issue_number")
        if isinstance(issue_number, int) and issue_number not in issue_numbers:
            issue_numbers.append(issue_number)
    assert issue_numbers, "timeline payload should include visible issue affordances"
    return issue_numbers


def _linked_issue_lifecycle_from_suite(
    timeline_payload: dict[str, Any],
    *,
    run_id: int,
    issue_number: int,
) -> tuple[dict[str, str], int]:
    """Validate the E2E suite lifecycle and summarize the target issue cycles."""
    suite = E2ESuiteTimelineContainer.model_validate(timeline_payload["lifecycle"])
    assert suite.subject.kind == "e2e_suite"
    assert len(suite.runs) == 1

    run_iteration = suite.runs[0]
    assert run_iteration.kind == "e2e_run"
    assert run_iteration.subject.kind == "e2e_run"
    assert run_iteration.subject.id == str(run_id)
    assert run_iteration.e2e_run.run_id == run_id
    assert run_iteration.e2e_run.tests
    assert run_iteration.e2e_run.linked_issue_lifecycles

    linked_from_tests = []
    for test_execution in run_iteration.e2e_run.tests:
        if test_execution.kind == "missing_e2e_test_evidence":
            continue
        linked_from_tests.extend(
            linked.issue_number for linked in test_execution.linked_issues
        )
    assert issue_number in linked_from_tests, (
        f"E2E lifecycle should link issue #{issue_number} from at least one test, "
        f"got {sorted(set(linked_from_tests))}"
    )

    target = next(
        (
            lifecycle
            for lifecycle in run_iteration.e2e_run.linked_issue_lifecycles
            if lifecycle.issue_number == issue_number
        ),
        None,
    )
    assert target is not None, (
        f"E2E lifecycle linked tests to issue #{issue_number}, but omitted its "
        "semantic issue lifecycle"
    )
    assert target.cycles

    first_cycle = target.cycles[0]
    assert first_cycle.coder.kind == "completed_coding_attempt"
    assert first_cycle.review.kind == "review_approved"
    assert first_cycle.coder.session_recording.kind == "available"
    assert first_cycle.coder.completion_record.kind == "available"
    assert any(
        command.kind == "open_session_recording"
        for command in first_cycle.coder.commands
    )
    assert any(
        command.kind == "show_event_details" for command in first_cycle.review.commands
    )

    return (
        {
            "coder_kind": first_cycle.coder.kind,
            "review_kind": first_cycle.review.kind,
            "outcome": first_cycle.outcome,
        },
        len(target.cycles),
    )


def _dashboard_lifecycle_summary(
    detail_payload: dict[str, Any],
    *,
    issue_number: int,
) -> tuple[dict[str, str], int]:
    """Validate the dashboard dummy-container lifecycle returned to the drawer."""
    dashboard = DashboardTimelineContainer.model_validate(detail_payload["lifecycle"])
    assert dashboard.subject.kind == "dashboard"
    assert dashboard.current.subject.kind == "dashboard"
    assert len(dashboard.current.issue_lifecycles) == 1

    lifecycle = dashboard.current.issue_lifecycles[0]
    assert lifecycle.issue_number == issue_number
    assert lifecycle.cycles
    first_cycle = lifecycle.cycles[0]
    assert first_cycle.coder.kind == "completed_coding_attempt"
    assert first_cycle.review.kind == "review_approved"

    return (
        {
            "coder_kind": first_cycle.coder.kind,
            "review_kind": first_cycle.review.kind,
            "outcome": first_cycle.outcome,
        },
        len(lifecycle.cycles),
    )


def _expected_rendered_runs(detail_payload: dict[str, Any]) -> list[dict[str, Any]]:
    runs = detail_payload["runs"]
    assert isinstance(runs, list) and runs, "issue detail payload should include runs"
    return [runs[-1]]


def _expected_rendered_step_narratives(detail_payload: dict[str, Any]) -> list[str]:
    narratives: list[str] = []
    for run in _expected_rendered_runs(detail_payload):
        for cycle in run["cycles"]:
            phase_groups = cycle.get("phase_groups") or [
                {"label": "", "steps": cycle.get("steps", [])}
            ]
            for group in phase_groups:
                for step in group.get("steps", []):
                    narratives.append(str(step["narrative"]).strip())
    assert narratives, "issue detail payload should render at least one journey step"
    return narratives


def _expected_rendered_phase_labels(detail_payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for run in _expected_rendered_runs(detail_payload):
        for cycle in run["cycles"]:
            for group in cycle.get("phase_groups") or []:
                label = str(group.get("label") or "")
                if label:
                    labels.append(label)
    return labels


def _assert_issue_detail_dom_matches_payload(
    page: Page,
    journey: Locator,
    detail_payload: dict[str, Any],
    *,
    expected_cycle_count: int,
) -> None:
    """Assert the rendered drawer is a faithful display of its rich payload."""
    expected_runs = _expected_rendered_runs(detail_payload)
    expected_dom_cycle_count = sum(len(run["cycles"]) for run in expected_runs)
    assert expected_dom_cycle_count == expected_cycle_count

    expect(journey.locator(".journey-run")).to_have_count(len(expected_runs))
    expect(journey.locator(".journey-cycle")).to_have_count(expected_dom_cycle_count)

    rendered_narratives = [
        text.strip()
        for text in journey.locator(
            ".journey-step .journey-narrative"
        ).all_text_contents()
    ]
    expected_narratives = _expected_rendered_step_narratives(detail_payload)
    assert rendered_narratives == expected_narratives, (
        "journey step narratives should render directly from the endpoint payload "
        f"without dropping/reordering semantic lifecycle steps:\n"
        f"expected={expected_narratives!r}\nactual={rendered_narratives!r}"
    )

    rendered_phase_labels = [
        text.strip()
        for text in journey.locator(".journey-phase-header").all_text_contents()
    ]
    assert rendered_phase_labels == _expected_rendered_phase_labels(detail_payload)

    run = expected_runs[0]
    run_header = journey.locator(".journey-run > .journey-cycle-header").first
    expect(run_header).to_contain_text(f"Run {run['run_number']}")
    # PR #6333: ``outcome`` is now a typed ``OutcomeBadge {label, tone}``.
    # The header displays the label; the tone drives the CSS class.
    expect(run_header).to_contain_text(run["outcome"]["label"])
    _expect_parseable_time_text(
        page,
        run_header.locator(".journey-cycle-time"),
        "issue-detail payload run timestamp",
    )


def _assert_issue_drawer_counts_match_payload(
    page: Page,
    detail_payload: dict[str, Any],
    *,
    issue_number: int,
) -> None:
    detail_drawer = page.locator("#issueDetailDrawer.visible")
    expect(detail_drawer).to_be_visible(timeout=15_000)
    expect(page.locator("#issueDetailTitle")).to_contain_text(
        f"Issue #{issue_number}",
        timeout=15_000,
    )

    journey = page.locator("#issueDetailJourney")
    expected_runs = _expected_rendered_runs(detail_payload)
    expected_cycles = sum(len(run["cycles"]) for run in expected_runs)
    expect(journey.locator(".journey-run")).to_have_count(len(expected_runs), timeout=15_000)
    expect(journey.locator(".journey-cycle")).to_have_count(expected_cycles, timeout=15_000)
    expect(journey.locator(".timeline-empty")).to_have_count(0, timeout=15_000)

    status_text = page.locator("#issueDetailStatus").text_content() or ""
    assert "Loading issue detail" not in status_text
    assert "Issue detail unavailable" not in status_text
    assert "Failed to load" not in status_text
    _assert_issue_detail_dom_matches_payload(
        page,
        journey,
        detail_payload,
        expected_cycle_count=expected_cycles,
    )
    _expect_all_parseable_time_texts(
        page,
        journey.locator(".journey-step .journey-time"),
        "issue-detail drawer step timestamp",
    )


def test_run_drawer_timeline_renders_clickable_issue_links(
    page: Page,
    fixture_web_server: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Validate row-level affordance placement and click-through content.

    Per the PR #5709 review, this test must prove:

    1. The Timeline tab actually rendered real event data from the
       endpoint payload — not just an empty skeleton. We look up the
       canonical ``test_4057`` event by its summary text and assert it
       exists before probing its issue-link content.
    2. The ``test_4057`` row carries exactly the two expected issue
       affordances (for run_88: 5723 + 5724, rendered as compact
       branch labels "inflight-discovery (5723)" and
       "ui-surface-provider-cir… (5724)"), scoped to that specific
       ``.timeline-event`` card so misattached links on the wrong row
       are caught.
    3. A neighboring row (``test_inflight_refresh_discovers_issue``)
       carries ONLY issue 5723 — a negative assertion that prevents
       false positives from cross-row contamination.
    4. Clicking the run-level ``#5723`` issue timeline control causes
       the issue-detail drawer to render journey content
       (``.journey-run`` and ``.journey-cycle``) with realistic coding
       and review milestones — not just an optimistic title that shows
       before the fetch completes.
    5. The session recording action for the staged coding event opens
       the replay modal and loads recognizable terminal output from a
       real synthetic ``terminal-recording.jsonl``.
    """
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]
    repo_root = fixture_web_server["repo_root"]
    caplog.set_level(
        "WARNING",
        logger="issue_orchestrator.entrypoints.timeline_presentation",
    )

    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(f"{base_url}/", wait_until="domcontentloaded", timeout=90_000)
    timeline_payload = _browser_fetch_json(
        page,
        _url(
            str(base_url),
            f"/control/e2e/run/{run_id}/timeline",
            repo_root=str(repo_root),
            config_name="default",
        ),
    )
    issue_detail_payload = _browser_fetch_json(
        page,
        _url(
            str(base_url),
            f"/api/e2e-run/{run_id}/issue-detail/{TEST_CLICK_ISSUE_NUMBER}",
            view="user",
        ),
    )
    suite_cycle_summary, suite_cycle_count = _linked_issue_lifecycle_from_suite(
        timeline_payload,
        run_id=int(run_id),
        issue_number=TEST_CLICK_ISSUE_NUMBER,
    )
    detail_cycle_summary, detail_cycle_count = _dashboard_lifecycle_summary(
        issue_detail_payload,
        issue_number=TEST_CLICK_ISSUE_NUMBER,
    )
    assert detail_cycle_summary == suite_cycle_summary
    assert detail_cycle_count == suite_cycle_count

    # Open the run view inline (#6334).  The row's summary click
    # dispatches the typed ``expand_e2e_run`` Command through the
    # shared dispatcher; the canonical viewer mounts inside
    # ``.e2e-run-row-content``.  Run id is on the row itself
    # (``data-e2e-run-id``) — no modal header to inspect.
    _open_e2e_tab(page)
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5000)
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", True)
    expect(row.locator(".e2e-run-row-id")).to_contain_text(
        f"Run #{run_id}", timeout=15_000,
    )
    # ``modal`` is kept as the local variable name so the deep
    # assertions below (canonical viewer mount, run-level affordances,
    # ARIA, etc.) read the same way they did pre-#6334.  It now means
    # "the expanded row body", not "the modal".
    modal = row

    # Phase C of #6310 follow-up: the canonical validation viewer is
    # the modal's body.  Run-level summary chips replace the legacy
    # headline; filter chips are gone.  ``cvv-root`` may have zero
    # visible bounding box when the run has no test cases — use a
    # presence check rather than visibility.
    expect(modal.locator(".cvv-root")).to_have_count(1)
    expect(modal.locator(".e2e-run-summary")).to_be_visible(timeout=15_000)

    # Run timeline is rendered eagerly inside the collapsed Diagnostics row.
    # Expand it to assert on its content.
    disclosure = modal.locator(".run-details-disclosure").first
    expect(disclosure).to_be_visible(timeout=15_000)
    disclosure.locator("summary").first.click()
    timeline_panel = disclosure.locator(".e2e-timeline-content").first
    expect(timeline_panel).to_be_visible(timeout=15_000)
    expect(timeline_panel).to_have_attribute(
        "data-lifecycle-kind",
        timeline_payload["lifecycle"]["kind"],
    )
    expect(timeline_panel).to_have_attribute(
        "data-lifecycle-iterations",
        str(len(timeline_payload["lifecycle"]["runs"])),
    )

    # Run-level issue timeline buttons must remain visible inside the
    # disclosure — they're the run-wide affordance for opening a tracked
    # issue's cycle drawer (vs. the per-test inline lifecycle in the row
    # expansion above).
    run_level_affordances = timeline_panel.locator(".e2e-issue-timeline-affordances")
    expect(run_level_affordances).to_be_visible(timeout=15_000)
    run_level_issue_btn = run_level_affordances.locator(
        ".e2e-issue-timeline-btn",
        has_text=f"#{TEST_CLICK_ISSUE_NUMBER}",
    ).first
    expect(run_level_issue_btn).to_be_visible(timeout=15_000)
    expect(run_level_issue_btn).to_contain_text(TEST_CLICK_ISSUE_LABEL)

    expected_run_level_issues = _issue_affordance_numbers(timeline_payload)
    run_level_issue_buttons = run_level_affordances.locator(".e2e-issue-timeline-btn")
    expect(run_level_issue_buttons).to_have_count(len(expected_run_level_issues))
    run_level_issue_text = "\n".join(run_level_issue_buttons.all_text_contents())
    for issue_number in expected_run_level_issues:
        assert f"#{issue_number}" in run_level_issue_text, (
            f"run-level issue affordance #{issue_number} is missing from: "
            f"{run_level_issue_text!r}"
        )

    # PR #6319 round 4: the affordance now routes through the shared
    # typed-Command pipeline.  Each button must carry a
    # ``data-lifecycle-command`` attribute with the right typed shape.
    # Without that, the click would not dispatch through the
    # ``runLifecycleCommand`` owner.
    for issue_number in expected_run_level_issues:
        button = run_level_affordances.locator(
            ".e2e-issue-timeline-btn",
            has_text=f"#{issue_number}",
        ).first
        cmd_attr = button.get_attribute("data-lifecycle-command") or ""
        assert cmd_attr, (
            f"run-level affordance button for issue #{issue_number} must carry "
            f"data-lifecycle-command"
        )
        cmd = json.loads(cmd_attr.replace("&quot;", '"').replace("&amp;", "&"))
        assert cmd.get("kind") == "open_issue_timeline"
        assert cmd.get("issue_number") == issue_number
        assert cmd.get("scope_kind") == "e2e_run"
        assert cmd.get("e2e_run_id") == int(run_id)

    # Spy on ``openIssueTimeline`` so we can prove the click chain
    # reaches the final UI dispatcher (not just the data-lifecycle-
    # command attribute on the button).  The spy must run before the
    # click loop below — install it once, then verify it was invoked
    # for each clicked affordance.
    page.evaluate(
        """() => {
            window.__openIssueTimelineCalls = [];
            const orig = window.openIssueTimeline;
            window.openIssueTimeline = (issueNumber, triggerEl, opts) => {
                window.__openIssueTimelineCalls.push({
                    issueNumber,
                    opts: opts || null,
                });
                if (typeof orig === 'function') {
                    orig(issueNumber, triggerEl, opts);
                }
            };
        }"""
    )

    for issue_number in expected_run_level_issues:
        per_issue_payload = _browser_fetch_json(
            page,
            _url(
                str(base_url),
                f"/api/e2e-run/{run_id}/issue-detail/{issue_number}",
                view="user",
            ),
        )
        per_issue_button = run_level_affordances.locator(
            ".e2e-issue-timeline-btn",
            has_text=f"#{issue_number}",
        ).first
        expect(per_issue_button).to_be_visible(timeout=5000)
        per_issue_button.click()
        # PR #6319 round 4: the click must reach the final UI
        # dispatcher.  The spy installed above captures every
        # ``openIssueTimeline`` call; the most recent entry must
        # match this issue + the e2e_run_id scope.
        calls = page.evaluate("() => window.__openIssueTimelineCalls")
        assert calls, (
            f"openIssueTimeline should fire when affordance #{issue_number} is clicked"
        )
        last = calls[-1]
        assert last["issueNumber"] == issue_number
        assert last["opts"] == {"e2eRunId": int(run_id)}, (
            f"affordance #{issue_number} must pass e2eRunId={run_id}; got opts={last['opts']!r}"
        )
        _assert_issue_drawer_counts_match_payload(
            page,
            per_issue_payload,
            issue_number=issue_number,
        )
        page.locator("#issueDetailCloseBtn").click()
        expect(page.locator("#issueDetailDrawer.visible")).to_have_count(
            0, timeout=5000
        )

    # --- Ask 4: prove the Timeline tab actually rendered real event data ---
    # Find the specific test_4057 event card by its summary text. If the
    # endpoint returned nothing, this fails fast — no amount of global
    # anchor matching would save us.
    test_4057_summary = timeline_panel.locator(
        ".timeline-event",
        has=page.locator(".timeline-summary", has_text=_TEST_4057_NODEID),
    )
    # Two summary rows (started + completed) share the nodeid in the
    # rendered timeline, so we scope assertions to the first occurrence.
    test_4057_event = test_4057_summary.first
    expect(test_4057_event).to_be_visible(timeout=5000)
    expect(test_4057_event.locator(".timeline-summary").first).to_contain_text(
        _TEST_4057_NODEID,
    )
    _expect_parseable_time_text(
        page,
        test_4057_event.locator(".timeline-time").first,
        "E2E test timeline row timestamp",
    )

    # Every rendered event row must expose an obvious overflow affordance
    # for details/actions. This catches the regression where the hint
    # referenced a "⋯" button but E2E timeline cards rendered no trigger
    # at all when they had no backend-provided actions.
    details_trigger = test_4057_event.locator(".timeline-event-menu-trigger").first
    expect(details_trigger).to_be_visible(timeout=5000)
    details_trigger.click()
    details_action = test_4057_event.locator(
        ".timeline-detail-action",
        has_text="Event Details",
    ).first
    expect(details_action).to_be_visible(timeout=5000)
    action_payload = details_action.get_attribute("data-action") or ""
    assert "detail_id" in action_payload, (
        f"event details action should reference a compact lookup id: {action_payload!r}"
    )
    assert _TEST_4057_NODEID not in action_payload, (
        "event details action should not duplicate the full event payload in data-action"
    )
    details_action.click()
    event_detail_modal = page.locator("#modalOverlay.visible")
    expect(event_detail_modal).to_be_visible(timeout=5000)
    expect(page.locator("#modalTitle")).to_contain_text("Timeline Event:")
    expect(page.locator("#modalBody")).to_contain_text(_TEST_4057_NODEID)
    expect(page.locator("#modalBody")).to_contain_text("Raw event JSON")
    page.locator("#modalOverlay .modal-close").first.click()
    expect(page.locator("#modalOverlay.visible")).to_have_count(0, timeout=5000)

    # --- Ask 1: scope link assertions to the test_4057 row ---
    # Affordances now render as "label (N)" with a hover title carrying
    # the full branch name. For run_88, test_4057 carries 5723 and 5724:
    #
    #   "inflight-discovery (5723)"        ← same issue as test_inflight
    #   "ui-surface-provider-cir… (5724)"  ← truncated by 24-char cap
    #
    test_4057_links = test_4057_event.locator(".timeline-issue-links a")
    expect(test_4057_links).to_have_count(2, timeout=5000)
    link_texts = sorted(test_4057_links.all_text_contents())
    expected_texts = sorted(
        [
            f"{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})",
            f"{TEST_CLICK_ISSUE_LABEL_2} ({TEST_CLICK_ISSUE_NUMBER_2})",
        ]
    )
    assert link_texts == expected_texts, (
        f"test_4057 row should carry {expected_texts!r} but got {link_texts!r}"
    )
    # Hover titles must carry the full (untruncated) branch name so users
    # can reclaim the detail hidden by the 24-char cap.
    for link in test_4057_links.all():
        title = link.get_attribute("title") or ""
        assert title.startswith(f"{TEST_CLICK_ISSUE_NUMBER}-") or title.startswith(
            f"{TEST_CLICK_ISSUE_NUMBER_2}-"
        ), f"test_4057 affordance is missing its full branch_name title: got {title!r}"

    # --- Ask 2: negative assertion on a neighboring row ---
    # test_inflight_refresh_discovers_issue carries ONLY issue 5723 per
    # the fixture's expected.json. Catch misattached links (e.g. 5724
    # leaking from test_4057) by asserting the link set.
    test_inflight_event = timeline_panel.locator(
        ".timeline-event",
        has=page.locator(".timeline-summary", has_text=_TEST_INFLIGHT_NODEID),
    ).first
    expect(test_inflight_event).to_be_visible(timeout=5000)
    inflight_links = test_inflight_event.locator(".timeline-issue-links a")
    expect(inflight_links).to_have_count(1, timeout=5000)
    inflight_texts = sorted(inflight_links.all_text_contents())
    assert inflight_texts == [
        f"{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})"
    ], (
        f"test_inflight_refresh row should carry only "
        f"[{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})] "
        f"but got {inflight_texts!r} — a link from another row has leaked in"
    )

    # --- Ask 3: click-through must prove the drawer loaded data ---
    # Click the run-level issue timeline affordance. Row-level issue
    # links were asserted above; this click-through pins the new
    # top-level control that makes the timeline discoverable.
    run_level_issue_btn.click()

    detail_drawer = page.locator("#issueDetailDrawer.visible")
    expect(detail_drawer).to_be_visible(timeout=5000)
    expect(page.locator("#issueDetailDrawer")).to_have_attribute(
        "data-lifecycle-kind",
        issue_detail_payload["lifecycle"]["kind"],
    )
    expect(page.locator("#issueDetailDrawer")).to_have_attribute(
        "data-lifecycle-iterations",
        "1",
    )
    # Title alone is not sufficient — it's set before the fetch completes.
    expect(page.locator("#issueDetailTitle")).to_contain_text(
        f"Issue #{TEST_CLICK_ISSUE_NUMBER}"
    )

    # Wait for the journey to actually render. The endpoint returns
    # events structured as runs/cycles, so on success #issueDetailJourney
    # contains at least one .journey-run. No .timeline-empty placeholder
    # should be present.
    journey = page.locator("#issueDetailJourney")
    expect(journey.locator(".journey-run").first).to_be_visible(timeout=5000)
    expect(journey.locator(".journey-cycle").first).to_be_visible(timeout=5000)
    _expect_all_parseable_time_texts(
        page,
        journey.locator(".journey-run > .journey-cycle-header .journey-cycle-time"),
        "issue-detail run timestamp",
    )
    _expect_all_parseable_time_texts(
        page,
        journey.locator(".journey-cycle > .journey-cycle-header .journey-cycle-time"),
        "issue-detail cycle timestamp",
    )
    _expect_all_parseable_time_texts(
        page,
        journey.locator(".journey-step .journey-time"),
        "issue-detail step timestamp",
    )
    expect(journey).to_contain_text("Agent finished coding")
    expect(journey).to_contain_text("Review approved")
    expect(journey.locator(".timeline-empty")).to_have_count(0)
    _assert_issue_detail_dom_matches_payload(
        page,
        journey,
        issue_detail_payload,
        expected_cycle_count=detail_cycle_count,
    )

    # The issue-detail drawer is transformed for its slide-in animation.
    # That makes fixed-position descendants use the drawer as their
    # coordinate container, so the journey overflow menu must compensate
    # or the "..." click appears to do nothing.
    coding_step = journey.locator(
        ".journey-step",
        has=page.locator(".timeline-action-btn", has_text="Coding Recording"),
    ).first
    expect(coding_step).to_be_visible(timeout=5000)
    coding_menu_trigger = coding_step.locator(".timeline-event-menu-trigger").first
    _dom_click_hit_tested(coding_menu_trigger, "coding journey overflow trigger")
    coding_menu_item = coding_step.locator(
        ".timeline-event-menu[open] .timeline-menu-item"
    ).first
    expect(coding_menu_item).to_be_visible(timeout=5000)
    menu_hit_result = coding_menu_item.evaluate(
        """
        (element) => {
            const rect = element.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(cx, cy);
            return {
                left: rect.left,
                right: rect.right,
                top: rect.top,
                bottom: rect.bottom,
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight,
                hitInside: !!hit && (hit === element || element.contains(hit)),
                hitClass: hit && hit.className ? String(hit.className) : "",
            };
        }
        """
    )
    assert (
        0 <= menu_hit_result["left"]
        < menu_hit_result["right"]
        <= menu_hit_result["viewportWidth"]
    ), f"coding journey overflow menu is horizontally unreachable: {menu_hit_result!r}"
    assert (
        0 <= menu_hit_result["top"]
        < menu_hit_result["bottom"]
        <= menu_hit_result["viewportHeight"]
    ), f"coding journey overflow menu is vertically unreachable: {menu_hit_result!r}"
    assert menu_hit_result["hitInside"], (
        f"coding journey overflow menu item is not hit-testable: {menu_hit_result!r}"
    )
    _dom_click_hit_tested(coding_menu_trigger, "coding journey overflow trigger close")
    expect(coding_step.locator(".timeline-event-menu[open]")).to_have_count(
        0, timeout=5000
    )

    # And the status line must no longer show the loading / unavailable text.
    status_el = page.locator("#issueDetailStatus")
    status_text = status_el.text_content() or ""
    assert "Loading issue detail" not in status_text, (
        f"drawer still showing loading state: {status_text!r}"
    )
    assert "Issue detail unavailable" not in status_text, (
        f"drawer showing unavailable state: {status_text!r}"
    )
    assert "Failed to load" not in status_text, (
        f"drawer showing failure state: {status_text!r}"
    )

    # The drawer no longer carries ambiguous Focus/GitHub issue-level
    # actions; timeline density is selected through the grouped chips.
    expect(page.locator("#issueDetailFocusBtn")).to_have_count(0)
    expect(page.locator("#issueDetailGitHubBtn")).to_have_count(0)
    expect(page.locator('#issueDetailDrawer [role="radiogroup"]')).to_have_count(0)
    expect(page.locator('#issueDetailDrawer [role="radio"]')).to_have_count(0)
    expect(page.locator("#issueDetailDrawer .journey-filter-group").first).to_be_visible()
    raw_events_btn = page.locator(
        "#issueDetailDrawer button",
        has_text="Raw events",
    )
    _dom_click_hit_tested(raw_events_btn, "issue detail raw events view")
    expect(raw_events_btn).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#issueDetailDrawer .journey-raw-event").first).to_be_visible(
        timeout=5000
    )
    story_btn = page.locator(
        "#issueDetailDrawer button",
        has_text="Story",
    )
    _dom_click_hit_tested(story_btn, "issue detail story view")
    expect(story_btn).to_have_attribute("aria-pressed", "true")
    expect(journey.locator(".journey-run").first).to_be_visible(timeout=5000)

    # --- Session Recording click-through ---
    # The fixture stager wired one agent.coding_started event for
    # issue 5705 at a real tmp_path run_dir with a synthetic
    # terminal-recording.jsonl. The action decorator therefore emits
    # a "Coding Recording" action on that event; click it and verify
    # the session-replay modal opens AND loads real terminal content
    # from the endpoint.
    #
    # Proving click-through is not enough: the modal title is set
    # optimistically before the fetch completes, same as the issue
    # detail drawer. We additionally assert
    #   (a) #sessionReplayPath carries the expected run_dir
    #   (b) sessionReplayState has > 0 events
    #   (c) the decoded events contain our synthetic recognizable text
    # so a future regression in the recording endpoint, the PTY event
    # decoding, or the replay initialization cannot silently pass.
    #
    # Issue #6334: the E2E run row stays open underneath any modal /
    # drawer the user opens from inside it — same intent as the
    # legacy modal-vs-drawer stacking contract, but mechanically much
    # simpler: the row is normal-flow content, drawers/modals stack
    # on top via their existing z-index rules.  Verify the row is
    # still ``[open]`` here.
    expect(row).to_have_js_property("open", True)

    session_recording_btn = journey.locator(
        ".timeline-event-actions > .timeline-action-btn",
        has_text="Coding Recording",
    ).first
    expect(session_recording_btn).to_be_visible(timeout=5000)
    session_recording_btn.scroll_into_view_if_needed()
    session_recording_btn.click()

    modal_overlay = page.locator("#modalOverlay.visible")
    expect(modal_overlay).to_be_visible(timeout=5000)

    session_replay_path = page.locator("#sessionReplayPath")
    expect(session_replay_path).to_be_visible(timeout=5000)
    session_path_text = session_replay_path.text_content() or ""
    assert (
        "session1" in session_path_text
        and "terminal-recording.jsonl" in session_path_text
    ), (
        f"session replay path does not reference the synthetic recording: "
        f"{session_path_text!r}"
    )

    # The progress readout shows "N / M events" once replay has
    # rendered the full stream. Poll until N > 0 so we're not racing
    # the initial fetch.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        text = page.locator("#sessionReplayProgressText").text_content() or ""
        if "/" in text and not text.startswith("0 /"):
            break
        page.wait_for_timeout(100)
    progress_text = page.locator("#sessionReplayProgressText").text_content() or ""
    assert "/" in progress_text, (
        f"session replay progress text not populated: {progress_text!r}"
    )
    assert not progress_text.startswith("0 /"), (
        f"session replay loaded zero events — endpoint returned empty "
        f"content: {progress_text!r}"
    )

    # Decode the raw events from sessionReplayState and verify the
    # synthetic output text round-tripped from the fixture through
    # the endpoint and back to the browser. This is what "reasonable
    # data" means for a session log in this test.
    #
    # ``sessionReplayState`` is declared with ``let`` at script scope
    # so it is NOT a property of ``window``; accessing it bare inside
    # the eval expression resolves the script-scoped binding.
    decoded_output = page.evaluate(
        """
        (() => {
            try {
                if (typeof sessionReplayState === 'undefined' || !sessionReplayState) {
                    return null;
                }
                const events = sessionReplayState.events || [];
                const chunks = [];
                for (const e of events) {
                    if (e && e.event_type === 'output' && e.data_b64) {
                        try {
                            chunks.push(atob(e.data_b64));
                        } catch (err) {
                            chunks.push(`<decode-error: ${err.message}>`);
                        }
                    }
                }
                return { count: events.length, text: chunks.join('') };
            } catch (err) {
                return { error: String(err) };
            }
        })()
        """
    )
    assert decoded_output is not None, (
        "sessionReplayState is not initialized after clicking Session Recording"
    )
    assert decoded_output.get("count", 0) >= 2, (
        f"session replay loaded fewer events than the fixture wrote "
        f"(expected >= 2 from 1 resize + 2 output, got "
        f"{decoded_output.get('count')})"
    )
    assert _SYNTHETIC_SESSION_OUTPUT in decoded_output.get("text", ""), (
        f"synthetic session text {_SYNTHETIC_SESSION_OUTPUT!r} not found in "
        f"decoded terminal output: {decoded_output.get('text')!r}"
    )
    assert not [
        record.message
        for record in caplog.records
        if "Timeline action decoration failed" in record.message
    ]
    assert _SYNTHETIC_SESSION_OUTPUT_FOLLOWUP in decoded_output.get("text", ""), (
        f"follow-up synthetic text {_SYNTHETIC_SESSION_OUTPUT_FOLLOWUP!r} "
        f"not found in decoded terminal output: {decoded_output.get('text')!r}"
    )

    assert errors == [], f"Unexpected page errors: {errors}"


def test_run_drawer_results_surface_run_evidence_and_linked_issue_sessions(
    page: Page,
    fixture_web_server: dict[str, object],
) -> None:
    """The default run view leads with tests; per-test expansion exposes the
    linked agentic cycle's coder session, review session, transcript, and
    validation actions inline. Raw artifact drill-downs live in the
    first-class "Run artifacts" section above the failure panel (issue
    #6593); the command and timeline diagnostics stay in the collapsed
    Diagnostics row at the bottom."""
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    run_detail_payload = _browser_fetch_json(
        page,
        _url(
            str(base_url),
            f"/api/e2e-run-detail/{run_id}",
            view="user",
        ),
    )
    suite = E2ESuiteTimelineContainer.model_validate(run_detail_payload["lifecycle"])
    run_lifecycle = suite.runs[0].e2e_run
    issue_lifecycle = next(
        lifecycle
        for lifecycle in run_lifecycle.linked_issue_lifecycles
        if lifecycle.issue_number == TEST_CLICK_ISSUE_NUMBER
    )
    latest_cycle = issue_lifecycle.cycles[-1]
    assert latest_cycle.coder.session_recording.kind == "available"

    # Issue #6334: open the run inline by clicking its row's summary.
    _open_e2e_tab(page)
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5000)
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", True)
    modal = row  # ``modal`` is the legacy variable name kept for diff clarity

    # Phase C: canonical viewer is mounted; run-summary chips visible.
    # ``cvv-root`` may have zero bounding box when the fixture has no
    # tests — use presence rather than visibility for the viewer.
    cvv = modal.locator(".cvv-root")
    expect(cvv).to_have_count(1, timeout=10_000)
    expect(modal.locator(".e2e-run-summary")).to_be_visible(timeout=10_000)

    # ── Real-data automated smoke: the canonical viewer rendered the
    #    actual fixture-backed payload, not just an empty shell. ────
    # Derive expectations from the run-detail payload the backend
    # actually served (no synthetic stub here).  Each failing
    # category contributes one triage card; each linked failure
    # carries an ``io.agent-context`` plugin block with the right
    # issue number; the Copy-error icon must appear on every failed
    # card.  This is what catches the wiring breaking between the
    # backend payload shape and the canonical viewer's render path.
    categories = (run_detail_payload or {}).get("results_by_category", {}) or {}
    failing_tests: list[dict] = []
    for key in ("untriaged", "has_issue", "flaky"):
        for test in categories.get(key) or []:
            failing_tests.append(test)
    linked_failing_tests = [
        t for t in failing_tests
        if t.get("existing_issue")
        and isinstance(t["existing_issue"].get("number"), int)
    ]
    passing_count = len(categories.get("passed") or []) + len(categories.get("fixed") or [])

    if failing_tests:
        # Triage card count must match the failing-test count.  The
        # canonical viewer renders one ``cvv-triage-card`` per failed
        # JUnit case, and the translator covers every failing
        # category.  A miscount here means the translator dropped a
        # failure.
        expect(cvv.locator(".cvv-triage-card")).to_have_count(len(failing_tests))
        # Every failure must surface the built-in Copy-error icon
        # (the canonical viewer's generic affordance — not plugin-
        # scoped).  A missing icon means the failure card has no
        # ``failure_details`` somehow, or the icon got dropped.
        expect(cvv.locator(".cvv-triage-card .cvv-copy-icon")).to_have_count(
            len(failing_tests)
        )

    if linked_failing_tests:
        # Every linked failure must carry the io.agent-context plugin
        # block with the right issue number.  If even one block is
        # missing, the translator forgot to emit ``extras`` for that
        # test.
        plugin_blocks = cvv.locator(".cvv-plugin.agent-context")
        expect(plugin_blocks).to_have_count(len(linked_failing_tests))
        for test in linked_failing_tests:
            issue_number = int(test["existing_issue"]["number"])
            issue_block = cvv.locator(
                ".cvv-plugin.agent-context",
                has_text=f"#{issue_number}",
            )
            expect(issue_block).to_have_count(1)
            # Issue #6322 follow-up: linked-failure drill-in is the
            # inline ``▸ Attempts on issue #N`` expander, backed by a
            # typed ``OpenInlineAgentAttemptsCommand``.  The legacy
            # ``open_issue_timeline`` teleport is gone.
            expect(
                issue_block.locator("button", has_text="Open issue drawer")
            ).to_have_count(0)
            expander = issue_block.locator(".agent-context-attempts-expander")
            expect(expander).to_have_count(1)
            assert expander.get_attribute("data-issue-number") == str(issue_number)
            cmd_attr = expander.get_attribute("data-lifecycle-command") or ""
            assert cmd_attr, (
                f"expander for #{issue_number} must carry data-lifecycle-command"
            )
            cmd = json.loads(cmd_attr.replace("&quot;", '"').replace("&amp;", "&"))
            assert cmd.get("kind") == "open_inline_agent_attempts"
            assert cmd.get("issue_number") == issue_number

    if passing_count > 0:
        # Passing tests live inside the browse-by-file expander.  The
        # row must exist and report the right total.
        browse_row = cvv.locator(".cvv-row-browse")
        expect(browse_row).to_have_count(1)
        expect(browse_row).to_contain_text(f"{passing_count} passed")

    # ARIA tree must be enhanced after mount (Phase D wire-up).  If
    # ``enhanceCanonicalValidationViewerAccessibility`` didn't run,
    # the dataset marker would be missing.
    expect(cvv).to_have_attribute("data-cvv-a11y-bound", "1")

    # Issue #6593: the raw artifact drill-downs (Raw Output, etc.) now live
    # in the first-class "Run artifacts" section above the failure panel —
    # reachable without expanding the collapsed Diagnostics row.
    artifacts = modal.locator(".e2e-run-artifacts").first
    expect(artifacts).to_be_visible(timeout=5000)
    expect(artifacts.locator("button", has_text="Raw Output")).to_be_visible(
        timeout=5000
    )

    # The run command + timeline diagnostics still live in the collapsed
    # Diagnostics row. Expand it and verify the command is reachable.
    disclosure = modal.locator(".run-details-disclosure").first
    expect(disclosure).to_be_visible(timeout=5000)
    disclosure.locator("summary").first.click()
    expect(disclosure.locator(".e2e-run-command")).to_contain_text(
        run_detail_payload["run"]["command"][0]
    )

    # The run-level issue affordance must be visible inside the timeline
    # panel and route to the cycle-aware issue drawer when clicked. The deep
    # journey traversal (cycle → coder session recording → terminal replay)
    # is exercised by test_run_drawer_timeline_renders_clickable_issue_links;
    # this test's job is the run-modal contract: headline + filter chips +
    # disclosure-housed timeline + issue affordances.
    timeline_panel = disclosure.locator(".e2e-timeline-content").first
    expect(timeline_panel).to_be_visible(timeout=5000)
    issue_btn = timeline_panel.locator(
        ".e2e-issue-timeline-btn",
        has_text=f"#{TEST_CLICK_ISSUE_NUMBER}",
    ).first
    expect(issue_btn).to_be_visible(timeout=5000)
    issue_btn.click()
    issue_drawer = page.locator("#issueDetailDrawer.visible")
    expect(issue_drawer).to_be_visible(timeout=5000)
    expect(page.locator("#issueDetailJourney").locator(".journey-cycle").first).to_be_visible(
        timeout=5000
    )
    # Coder session metadata is part of the issue lifecycle payload; the
    # cycle's coder.session_recording must remain "available" so the deep
    # path (a separate test exercises) keeps working.
    assert latest_cycle.coder.session_recording.kind == "available"


def test_run_drawer_results_render_generic_artifacts_without_linked_issue_lifecycle(
    page: Page,
    fixture_web_server: dict[str, object],
) -> None:
    """Generic command-runner runs must still surface artifacts without issue links."""
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    run_detail_payload = _browser_fetch_json(
        page,
        _url(
            str(base_url),
            f"/api/e2e-run-detail/{run_id}",
            view="user",
        ),
    )
    synthetic_payload = json.loads(json.dumps(run_detail_payload))
    synthetic_payload["run"]["runner_kind"] = "command"
    synthetic_payload["run"]["status"] = "passed"
    synthetic_payload["run"]["command"] = ["sh", "scripts/run-e2e-suite.sh"]
    synthetic_payload["run"]["log_path"] = "/tmp/tixmeup-e2e-worker.log"
    synthetic_payload["reports"] = [
        {
            "kind": "junit_xml",
            "label": "JUnit XML: tixmeup-e2e-smoke.xml",
            "path": "/tmp/tixmeup-e2e-smoke.xml",
        }
    ]
    adversarial_artifact_path = (
        "/tmp/compose \"quotes\" <tag> back\\slash 'apostrophe'.log"
    )
    synthetic_payload["artifacts"] = [
        {
            "kind": "text_artifact",
            "label": "Text Artifact: compose-services.log",
            "path": adversarial_artifact_path,
        },
        {
            "kind": "text_artifact",
            "label": "Text Artifact: tixmeup-e2e-smoke.summary.txt",
            "path": "/tmp/tixmeup-e2e-smoke.summary.txt",
        },
    ]
    suite_lifecycle = synthetic_payload["lifecycle"]
    suite_lifecycle["runs"][0]["e2e_run"]["linked_issue_lifecycles"] = []
    synthetic_payload["issue_affordances"] = []
    synthetic_payload["results_by_category"] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [
            {
                "nodeid": "tixmeup.e2e.smoke::package.build_image",
                "display_name": "package.build_image",
                "suite_name": "tixmeup.e2e.smoke",
                "outcome": "passed",
                "retry_outcome": None,
                "duration_seconds": 450.0,
                "longrepr": None,
                "history": [],
                "existing_issue": None,
                "flip_rate_percent": 0,
                "category": "healthy",
                "result_category": "passed",
                "result_source": "junit_xml",
                "is_quarantined": False,
            }
        ],
        "quarantined": [],
        "skipped": [],
    }
    synthetic_payload["results_summary"] = {
        "total": 1,
        "passed": 1,
        "untriaged": 0,
        "has_issue": 0,
        "flaky": 0,
        "fixed": 0,
        "quarantined": 0,
        "skipped": 0,
    }

    page.route(
        _url(
            str(base_url),
            f"/api/e2e-run-detail/{run_id}",
            view="user",
        ),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(synthetic_payload),
        ),
    )
    # Issue #6334: open the run inline by clicking its row's summary.
    _open_e2e_tab(page)
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5000)
    page.evaluate(
        """() => {
            window.__openedPaths = [];
            window.openPath = (path) => window.__openedPaths.push(String(path));
        }"""
    )
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", True)
    modal = row  # legacy variable name kept for diff clarity

    # Phase C: the canonical viewer holds the test list.  For a
    # single passing test the body's status is passed and the test
    # name appears in the rendered HTML (inside browse-by-file).
    cvv = modal.locator(".cvv-root").first
    expect(cvv).to_be_visible(timeout=5000)
    expect(cvv).to_have_attribute("data-cvv-status", "passed")
    expect(cvv).to_contain_text("package.build_image")

    # Issue #6593: the raw artifact buttons live in the first-class "Run
    # artifacts" section above the failure panel — reachable without
    # expanding the collapsed Diagnostics row.
    artifacts = modal.locator(".e2e-run-artifacts").first
    expect(artifacts).to_be_visible(timeout=5000)

    raw_output_btn = artifacts.locator("button", has_text="Raw Output").first
    junit_btn = artifacts.locator(
        "button", has_text="JUnit XML: tixmeup-e2e-smoke.xml"
    ).first
    compose_log_btn = artifacts.locator(
        "button", has_text="Text Artifact: compose-services.log"
    ).first
    summary_btn = artifacts.locator(
        "button", has_text="Text Artifact: tixmeup-e2e-smoke.summary.txt"
    ).first
    expect(raw_output_btn).to_be_visible(timeout=5000)
    expect(junit_btn).to_be_visible(timeout=5000)
    expect(compose_log_btn).to_be_visible(timeout=5000)
    expect(summary_btn).to_be_visible(timeout=5000)

    _dom_click_hit_tested(raw_output_btn, "generic run raw output button")
    _dom_click_hit_tested(junit_btn, "generic run junit report button")
    _dom_click_hit_tested(compose_log_btn, "generic run compose log button")
    _dom_click_hit_tested(summary_btn, "generic run summary artifact button")

    # The run command still lives in the collapsed Diagnostics row.
    disclosure = modal.locator(".run-details-disclosure").first
    expect(disclosure).to_be_visible(timeout=5000)
    disclosure.locator("summary").first.click()
    expect(disclosure.locator(".e2e-run-command")).to_contain_text(
        "sh scripts/run-e2e-suite.sh"
    )

    opened_paths = page.evaluate("() => window.__openedPaths.slice()")
    assert opened_paths == [
        "/tmp/tixmeup-e2e-worker.log",
        "/tmp/tixmeup-e2e-smoke.xml",
        adversarial_artifact_path,
        "/tmp/tixmeup-e2e-smoke.summary.txt",
    ]


def test_run_history_view_results_opens_formatted_passed_run_modal(
    page: Page,
    fixture_web_server: dict[str, object],
) -> None:
    """Run-history row click opens the canonical viewer inline (#6334).

    The legacy SSR "View Results" button per row was retired with the
    modal — the row's summary is now the click target, dispatching the
    typed ``expand_e2e_run`` Command through the shared dispatcher.
    """
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _route_synthetic_passed_run_detail(page, str(base_url), run_id)
    _open_e2e_tab(page)
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5000)
    row.locator("summary").first.click()

    _expect_formatted_passed_run_modal(page, run_id)


def test_latest_view_results_opens_formatted_passed_run_modal(
    page: Page,
    fixture_web_server: dict[str, object],
) -> None:
    """Latest-run View Results uses live E2E state and opens formatted results."""
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _route_synthetic_passed_run_detail(page, str(base_url), run_id)
    _open_e2e_tab(page)

    page.evaluate(
        """(runId) => {
            e2eLastRun = { id: runId, status: 'passed' };
            window.dashboardData.e2eLastRun = e2eLastRun;
        }""",
        run_id,
    )
    latest_results = page.locator(".e2e-last-results-btn")
    expect(latest_results).to_be_visible(timeout=5000)
    expect(latest_results).to_be_enabled(timeout=5000)
    expect(latest_results).to_have_text("View Results")
    latest_results.click()

    _expect_formatted_passed_run_modal(page, run_id)


def test_run_modal_canonical_viewer_shows_failures_passes_and_linked_issue_plugin(
    page: Page,
    fixture_web_server: dict[str, object],
) -> None:
    """Phase C of #6310 follow-up: the test-centric layout that this
    test used to exercise (filter chips + per-row triage actions +
    per-row lifecycle block) was replaced by the canonical validation
    viewer.  This rewrite pins the equivalent Phase C behaviors:

    - The canonical viewer renders both tests with the right outcome
      shape (failed triage card on top + passing test inside browse-by-file).
    - The failure card shows the specific longrepr text (catches
      regressions where the wrong field is wired or text gets escaped).
    - The built-in Copy-error icon writes the failure text to the
      clipboard (no per-row Copy Error button — same capability,
      different surface).
    - The linked failure carries the ``io.agent-context`` plugin
      block with the right issue number and the inline ``▸ Attempts
      on issue #N`` expander (issue #6322 follow-up).  Per-row Coder
      Session / Timeline / Review buttons are intentionally gone —
      the inline drill-in is the single entry point now.
    - Filter chips are intentionally gone (cut in Phase C).
    """
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    real_payload = _browser_fetch_json(
        page,
        _url(str(base_url), f"/api/e2e-run-detail/{run_id}", view="user"),
    )

    expected_longrepr = (
        "AssertionError: expected primary search to return product slug "
        "'butter-yellow-blanket' but got 'mustard-throw'"
    )
    expected_passed_label = "package.build_image"
    expected_failed_label = "search_returns_expected_slug"
    failed_nodeid = "tixmeup.e2e.search::failures.search_returns_expected_slug"
    passed_nodeid = "tixmeup.e2e.smoke::package.build_image"

    failing_test = {
        "nodeid": failed_nodeid,
        "case_id": failed_nodeid,
        "label": expected_failed_label,
        "display_name": expected_failed_label,
        "suite_name": "tixmeup.e2e.search",
        "outcome": "failed",
        "retry_outcome": None,
        "duration_seconds": 1.42,
        "longrepr": expected_longrepr,
        "failure_summary": "AssertionError: expected primary search…",
        "history": [],
        "existing_issue": {
            "number": 5723,
            "status": "open",
            "resolution": None,
        },
        "category": "new_failure",
        "result_category": "has_issue",
        "flip_rate": 0.0,
        "flip_rate_percent": 0.0,
        "is_likely_flaky": False,
        "is_quarantined": False,
        "result_source": "junit",
        "updated_at": "",
    }
    passed_test = {
        "nodeid": passed_nodeid,
        "case_id": passed_nodeid,
        "label": expected_passed_label,
        "display_name": expected_passed_label,
        "suite_name": "tixmeup.e2e.smoke",
        "outcome": "passed",
        "retry_outcome": None,
        "duration_seconds": 450.0,
        "longrepr": None,
        "failure_summary": None,
        "history": [],
        "existing_issue": None,
        "category": "healthy",
        "result_category": "passed",
        "flip_rate": 0.0,
        "flip_rate_percent": 0.0,
        "is_likely_flaky": False,
        "is_quarantined": False,
        "result_source": "junit",
        "updated_at": "",
    }

    synthetic = json.loads(json.dumps(real_payload))
    synthetic["results_by_category"] = {
        "untriaged": [],
        "has_issue": [failing_test],
        "flaky": [],
        "fixed": [],
        "passed": [passed_test],
        "quarantined": [],
        "skipped": [],
    }
    synthetic["results_summary"] = {
        "total": 2,
        "passed": 1,
        "untriaged": 0,
        "has_issue": 1,
        "flaky": 0,
        "fixed": 0,
        "quarantined": 0,
        "skipped": 0,
    }

    page.route(
        _url(str(base_url), f"/api/e2e-run-detail/{run_id}", view="user"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(synthetic),
        ),
    )
    # Issue #6334: open the run inline by clicking its row's summary.
    _open_e2e_tab(page)
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5000)
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", True)
    modal = row  # legacy variable name kept for diff clarity

    # ── Canonical viewer mounted with the right shape ─────────────────
    cvv = modal.locator(".cvv-root").first
    expect(cvv).to_be_visible(timeout=5000)
    expect(cvv).to_have_attribute("data-cvv-status", "failed")

    # ── Run-level summary chips show 1 failing + 1 passing ────────────
    summary = modal.locator(".e2e-run-summary")
    expect(summary).to_contain_text("1 failing")
    expect(summary).to_contain_text("1 passing")

    # Phase D outcome groups: the Failed group is collapsed; open it
    # first to reveal the triage cards inside.
    failed_group = cvv.locator(".cvv-group-failed")
    expect(failed_group).to_have_count(1)
    failed_group.locator("summary").first.click()

    # ── Failure triage card carries the right name + longrepr ─────────
    failure_card = cvv.locator(".cvv-triage-card", has_text=expected_failed_label)
    expect(failure_card).to_be_visible(timeout=2000)

    # Phase D: the failure card is a <details> closed by default.  Open
    # it to reveal the traceback / plugin / leaf rows so the
    # assertions below see their targets visible.
    failure_card.locator("summary").first.click()
    # The traceback row inside the card holds the specific longrepr.
    # It still starts closed (predictable-collapse rule) — but the
    # text is in the DOM regardless of open state, so to_contain_text
    # finds it.
    expect(failure_card).to_contain_text(expected_longrepr)

    # ── Built-in Copy-error icon writes the failure text to clipboard ─
    # The Copy-error icon lives in the card's summary row, so it's
    # visible even when the card is closed.  But the failure card
    # body is now open (we clicked summary above), so we can test the
    # full path.
    page.evaluate(
        """() => {
            window.__copiedE2EText = "";
            Object.defineProperty(navigator, "clipboard", {
                configurable: true,
                value: {
                    writeText: async (text) => { window.__copiedE2EText = String(text); },
                },
            });
        }"""
    )
    copy_icon = failure_card.locator(".cvv-copy-icon").first
    expect(copy_icon).to_be_visible()
    copy_icon.click()
    copied_text = page.evaluate("() => window.__copiedE2EText")
    assert expected_longrepr in copied_text, (
        f"Copy-error icon must copy the failure detail; got {copied_text!r}"
    )

    # ── Linked-failure plugin block carries the issue number + inline ─
    # ``▸ Attempts on issue #N`` expander backed by a typed Command
    # (issue #6322 follow-up).  Inside the now-open card body.
    plugin = failure_card.locator(".cvv-plugin.agent-context")
    expect(plugin).to_be_visible()
    expect(plugin).to_contain_text("#5723")
    expect(plugin.locator("button", has_text="Open issue drawer")).to_have_count(0)
    expander = plugin.locator(".agent-context-attempts-expander")
    expect(expander).to_have_count(1)
    assert expander.get_attribute("data-issue-number") == "5723"
    expect(expander).to_contain_text("Attempts on issue #5723")
    cmd_attr = expander.get_attribute("data-lifecycle-command") or ""
    assert cmd_attr, "expander must carry data-lifecycle-command"
    cmd = json.loads(cmd_attr.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd.get("kind") == "open_inline_agent_attempts"
    assert cmd.get("issue_number") == 5723

    # E2E click-through proof (piggybacks on the existing run-modal
    # test): stub ``fetch`` and prove the inline expander's
    # ``runLifecycleCommandFromToggle`` dispatch lands on the
    # ops-view issue-detail URL.  Catches a regression where the
    # markup is right but the dispatcher's branch breaks.
    page.evaluate(
        "() => {"
        "  window.__inlineAttemptsCalls = [];"
        "  window.fetch = (url) => {"
        "    window.__inlineAttemptsCalls.push(String(url));"
        "    return Promise.resolve({ ok: true, status: 200, "
        "      json: () => Promise.resolve({ runs: [] }), "
        "    });"
        "  };"
        "}"
    )
    expander.locator("summary").first.click()
    page.wait_for_function(
        "() => (window.__inlineAttemptsCalls || []).length > 0",
        timeout=5000,
    )
    inline_calls = page.evaluate("() => window.__inlineAttemptsCalls")
    assert any("/api/issue-detail/5723?view=ops" in url for url in inline_calls), (
        f"expected lazy fetch of /api/issue-detail/5723?view=ops via the typed "
        f"Command dispatcher; got: {inline_calls}"
    )

    # ── Passing test renders inside the canonical viewer (browse-by-file) ─
    expect(cvv).to_contain_text(expected_passed_label)

    # ── No legacy filter chips or per-row triage UI anywhere in the
    #    modal — these were all Phase C cuts. ────────────────────────
    expect(modal.locator(".trf-chip")).to_have_count(0)
    expect(modal.locator(".test-results-headline")).to_have_count(0)
    expect(modal.locator(".trr-row")).to_have_count(0)
    # Phase C deferred lazy captured-output fetch for the canonical
    # viewer (the canonical viewer renders stdout/stderr rows itself,
    # populated from whatever's on the JUnit case payload).  Lazy-fetch
    # for the canonical viewer is its own follow-up.
    expect(modal.locator(".trr-captured-output")).to_have_count(0)


def test_timeline_renderer_surfaces_unhappy_states_and_diagnostics(
    page: Page,
    fixture_web_server: dict[str, object],
) -> None:
    base_url = fixture_web_server["url"]
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(f"{base_url}/", wait_until="domcontentloaded", timeout=90_000)
    synthetic_events = [
        {
            "event_id": "unhappy-blocked",
            "timestamp": "2026-04-21T13:00:00Z",
            "event": "agent.blocked",
            "phase": "coding",
            "step": "blocked",
            "status": "blocked",
            "summary": "Blocked coder: waiting for product decision",
            "actions": [
                {
                    "type": "open_session_diagnostics",
                    "label": "Diagnostics",
                    "issue_number": 9001,
                    "run_dir": "/tmp/unhappy-coder",
                }
            ],
        },
        {
            "event_id": "unhappy-coding-running",
            "timestamp": "2026-04-21T13:02:00Z",
            "event": "agent.coding_started",
            "phase": "coding",
            "step": "started",
            "status": "in_progress",
            "summary": "Running coder: implementation in progress",
        },
        {
            "event_id": "unhappy-coding-failed",
            "timestamp": "2026-04-21T13:04:00Z",
            "event": "agent.failed",
            "phase": "coding",
            "step": "failed",
            "status": "failed",
            "summary": "Failed coder: session exited with error",
        },
        {
            "event_id": "unhappy-publish",
            "timestamp": "2026-04-21T13:05:00Z",
            "event": "publish.failed",
            "phase": "publish",
            "step": "failed",
            "status": "failed",
            "summary": "Publish failed: push rejected",
        },
        {
            "event_id": "unhappy-review-running",
            "timestamp": "2026-04-21T13:10:00Z",
            "event": "review.started",
            "phase": "review",
            "step": "started",
            "status": "in_progress",
            "summary": "Review running: reviewer session active",
        },
        {
            "event_id": "unhappy-review-changes",
            "timestamp": "2026-04-21T13:12:00Z",
            "event": "review.changes_requested",
            "phase": "review",
            "step": "changes_requested",
            "status": "changes_requested",
            "summary": "Review changes requested: add a regression test",
        },
        {
            "event_id": "unhappy-review-failed",
            "timestamp": "2026-04-21T13:15:00Z",
            "event": "review_exchange.failed",
            "phase": "review",
            "step": "failed",
            "status": "failed",
            "summary": "Review failed: reviewer crashed",
        },
        {
            "event_id": "unhappy-review-skipped",
            "timestamp": "2026-04-21T13:17:00Z",
            "event": "review.skipped",
            "phase": "review",
            "step": "skipped",
            "status": "skipped",
            "summary": "Review skipped: review disabled for this issue",
        },
        {
            "event_id": "unhappy-review-not-reached",
            "timestamp": "2026-04-21T13:18:00Z",
            "event": "semantic.review_not_reached",
            "phase": "review",
            "step": "not_reached",
            "status": "not_started",
            "summary": "Review not reached: coding failed first",
        },
        {
            "event_id": "unhappy-missing-coding",
            "timestamp": "2026-04-21T13:19:00Z",
            "event": "semantic.missing_coding_evidence",
            "phase": "diagnostics",
            "step": "missing_coding_evidence",
            "status": "failed",
            "summary": "Missing coding evidence: no coding start event",
        },
        {
            "event_id": "unhappy-missing-evidence",
            "timestamp": "2026-04-21T13:20:00Z",
            "event": "semantic.missing_evidence",
            "phase": "diagnostics",
            "step": "missing_evidence",
            "status": "validation_failed",
            "summary": "Missing evidence: completion_record was not emitted",
            "actions": [
                {
                    "type": "show_actions_error",
                    "label": "What is missing?",
                    "issue_number": 9001,
                    "error_message": "completion_record missing",
                    "error_messages": ["completion_record missing"],
                }
            ],
        },
        {
            "event_id": "unhappy-missing-review",
            "timestamp": "2026-04-21T13:22:00Z",
            "event": "semantic.missing_review_evidence",
            "phase": "diagnostics",
            "step": "missing_review_evidence",
            "status": "failed",
            "summary": "Missing review evidence: required review stage absent",
        },
    ]
    render_result = page.evaluate(
        """
        (events) => {
            if (typeof renderTimeline !== 'function') {
                throw new Error('renderTimeline is not available');
            }
            const existing = document.getElementById('synthetic-unhappy-timeline');
            if (existing) existing.remove();
            const container = document.createElement('section');
            container.id = 'synthetic-unhappy-timeline';
            document.body.appendChild(container);
            renderTimeline(
                container,
                events,
                [
                    { phase: 'coding', label: 'Coding' },
                    { phase: 'publish', label: 'Publish' },
                    { phase: 'review', label: 'Review' },
                    { phase: 'diagnostics', label: 'Diagnostics' },
                ],
                [
                    {
                        cycle: 1,
                        phases: ['coding', 'publish', 'review', 'diagnostics'],
                        status: 'validation_failed',
                    },
                ],
            );
            return {
                text: container.innerText,
                eventCount: container.querySelectorAll('.timeline-event').length,
                emptyCount: container.querySelectorAll('.timeline-empty').length,
            };
        }
        """,
        synthetic_events,
    )

    assert render_result["eventCount"] == len(synthetic_events)
    assert render_result["emptyCount"] == 0
    rendered_text = render_result["text"]
    for expected_text in (
        "Blocked coder: waiting for product decision",
        "Running coder: implementation in progress",
        "Failed coder: session exited with error",
        "Publish failed: push rejected",
        "Review running: reviewer session active",
        "Review changes requested: add a regression test",
        "Review failed: reviewer crashed",
        "Review skipped: review disabled for this issue",
        "Review not reached: coding failed first",
        "Missing coding evidence: no coding start event",
        "Missing evidence: completion_record was not emitted",
        "Missing review evidence: required review stage absent",
        "Validation Failed",
    ):
        assert expected_text in rendered_text

    synthetic = page.locator("#synthetic-unhappy-timeline")
    rendered_summaries = [
        text.strip()
        for text in synthetic.locator(".timeline-summary").all_text_contents()
    ]
    assert rendered_summaries == [
        str(event["summary"]) for event in synthetic_events
    ], "synthetic timeline events should render in payload order"
    _expect_all_parseable_time_texts(
        page,
        synthetic.locator(".timeline-time"),
        "synthetic unhappy timeline timestamp",
    )
    missing_event = synthetic.locator(
        ".timeline-event",
        has=page.locator(
            ".timeline-summary",
            has_text="Missing evidence: completion_record was not emitted",
        ),
    ).first
    expect(missing_event).to_be_visible(timeout=5000)
    missing_event.locator(".timeline-event-menu-trigger").click()
    missing_event.locator(
        ".timeline-menu-item",
        has_text="What is missing?",
    ).click()
    expect(page.locator("#modalOverlay.visible")).to_be_visible(timeout=5000)
    expect(page.locator("#modalTitle")).to_contain_text("What is missing #9001")
    expect(page.locator("#modalBody")).to_contain_text("completion_record missing")

    assert errors == [], f"Unexpected page errors: {errors}"
