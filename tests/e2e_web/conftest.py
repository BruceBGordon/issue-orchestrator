"""Playwright fixtures for Flow-first dashboard smoke tests."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
import os
import re
import shlex
import socket
import time
from threading import Thread
from typing import Final
from unittest.mock import MagicMock

import pytest
import uvicorn
from playwright.sync_api import ConsoleMessage, Page, Request

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.entrypoints.cli_tools.validate_runner import (
    find_worktree_root,
    get_output_dir,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from issue_orchestrator.infra.config import Config
import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from issue_orchestrator.ports.timeline_store import TimelineRecord
from tests.fixtures.timeline_run_artifacts import write_available_timeline_run_manifest
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


# ---------------------------------------------------------------------------
# Browser evidence on failure (#7140)
#
# A browser flake that only reproduces under load is undiagnosable from a
# pytest traceback: the traceback says "did not find some options" but not
# what the page actually contained, which requests were in flight, or what
# the console said. pytest-playwright already implements retain-on-failure
# tracing and only-on-failure screenshots — it just defaults them off and
# writes them to ``test-results/``. This section turns them on for the
# browser lane and redirects the artifacts into the diagnostics tree the
# gate's retention machinery already keeps, plus a plain-text console
# sidecar so an agent can read the evidence without a trace viewer.
#
# Cost on a green run: the trace/screenshot are captured to a temp dir and
# deleted at teardown, so nothing is retained and nothing is written to the
# repo. Only failures leave artifacts behind.
# ---------------------------------------------------------------------------

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_EVENT_LINES = 500

@dataclass(frozen=True, slots=True)
class ArtifactOption:
    """One pytest-playwright artifact switch this lane has an opinion about.

    ``flag`` is the CLI spelling the owning plugin registers for ``dest``;
    ``test_declared_flags_match_the_live_parser`` pins that mapping against
    a real parse so a rename cannot pass silently.
    """

    flag: str
    failure_only: str
    capturing: frozenset[str]


ARTIFACT_OPTIONS: Final[Mapping[str, ArtifactOption]] = {
    "tracing": ArtifactOption(
        flag="--tracing",
        failure_only="retain-on-failure",
        capturing=frozenset({"on", "retain-on-failure"}),
    ),
    "screenshot": ArtifactOption(
        flag="--screenshot",
        failure_only="only-on-failure",
        capturing=frozenset({"on", "only-on-failure"}),
    ),
}

_UNSUPPLIED: Final = object()


def registered_artifact_dests(config: pytest.Config) -> frozenset[str]:
    """Which artifact options this pytest run actually has.

    ``-p no:playwright`` leaves them unregistered: there is then nothing to
    supply and nothing to default. ``Config.getoption`` answers this on the
    public API — it returns the sentinel rather than raising for an option
    it does not know.
    """
    return frozenset(
        dest
        for dest in ARTIFACT_OPTIONS
        if config.getoption(dest, default=_UNSUPPLIED) is not _UNSUPPLIED
    )


def effective_pytest_args(config: pytest.Config) -> tuple[str, ...]:
    """Every token pytest parsed options out of, from all supply channels.

    ``Config.parse`` reads options from three places — the ``addopts`` ini
    key, the ``PYTEST_ADDOPTS`` environment variable, and the invocation
    argv — and merges them before parsing. Supplied-ness has to be judged
    against that same union; the parsed *value* cannot answer it, because an
    operator who explicitly asks for the default value is indistinguishable
    from one who asked for nothing.
    """
    return (
        *config.getini("addopts"),
        *shlex.split(os.environ.get("PYTEST_ADDOPTS", "")),
        *config.invocation_params.args,
    )


def supplied_option_dests(args: Sequence[str]) -> frozenset[str]:
    """Which artifact options the operator actually spelled out in ``args``.

    Re-parses the token stream with a probe parser whose defaults are a
    private sentinel, so "seen" and "absent" stay distinguishable no matter
    what value was given — which is the whole point, since the value an
    operator asks for may equal the default. ``--opt value`` and
    ``--opt=value`` parse identically to pytest because argparse is doing
    the parsing.

    A token that merely *looks* like one of these options (say, ``-k
    --tracing``) counts as supplied. That errs toward leaving the operator's
    configuration alone, which is the safe direction.
    """
    probe = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    for dest, spec in ARTIFACT_OPTIONS.items():
        probe.add_argument(spec.flag, dest=dest, default=_UNSUPPLIED)
    namespace, _ = probe.parse_known_args(list(args))
    return frozenset(
        dest for dest in ARTIFACT_OPTIONS if getattr(namespace, dest) is not _UNSUPPLIED
    )


def apply_failure_only_artifact_defaults(
    option: argparse.Namespace,
    *,
    supplied: frozenset[str],
    registered: frozenset[str],
) -> None:
    """Move registered-but-unsupplied artifact options to failure-only mode."""
    for dest, spec in ARTIFACT_OPTIONS.items():
        if dest in supplied or dest not in registered:
            continue
        setattr(option, dest, spec.failure_only)


def captures_failure_evidence(option: argparse.Namespace) -> bool:
    """Whether the resolved policy retains anything when a test fails.

    False means every artifact is off, so this lane writes nothing at all —
    including its own console sidecar.
    """
    return any(
        getattr(option, dest, "off") in spec.capturing
        for dest, spec in ARTIFACT_OPTIONS.items()
    )


def pytest_configure(config: pytest.Config) -> None:
    """Default the browser lane to failure-only Playwright artifacts.

    Set here rather than in the Makefile so a bare
    ``pytest tests/e2e_web/...`` captures the same evidence the gate does —
    the reproduction loop for a browser flake is exactly that bare
    invocation.

    Only *unsupplied* options are defaulted. ``--tracing=off`` and "no
    ``--tracing`` at all" both leave ``config.option.tracing == "off"``, so
    supplied-ness is resolved from the token stream the option came from,
    never from the value it ended up with — an operator who explicitly turns
    an artifact off must not have it resolved back on.
    """
    apply_failure_only_artifact_defaults(
        config.option,
        supplied=supplied_option_dests(effective_pytest_args(config)),
        registered=registered_artifact_dests(config),
    )


def browser_evidence_dir(nodeid: str) -> Path:
    """Directory that holds one failing browser test's evidence.

    Anchored on the same resolution ``validate_runner`` uses for pytest
    text output (``ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR`` when the
    orchestrator runs the gate, ``<worktree>/.issue-orchestrator/diagnostics``
    otherwise) so browser evidence is retained and pruned with everything
    else from the same run.
    """
    root = get_output_dir(find_worktree_root()) / "e2e-web"
    return root / _UNSAFE_PATH_CHARS.sub("-", nodeid).strip("-")


@pytest.fixture
def output_path(request: pytest.FixtureRequest) -> str:
    """Redirect pytest-playwright's trace/screenshot into the diagnostics tree.

    Overrides the plugin's own ``output_path`` fixture. Deliberately does
    NOT change ``--output``: pytest-playwright ``rmtree()``s that directory
    at session start, which must never point at the diagnostics tree.
    """
    return str(browser_evidence_dir(request.node.nodeid))


def _format_console(message: ConsoleMessage) -> str:
    location = message.location or {}
    where = f"{location.get('url', '')}:{location.get('lineNumber', '')}"
    return f"[console:{message.type}] {message.text}  ({where})"


@pytest.fixture
def page(page: Page, request: pytest.FixtureRequest) -> Iterator[Page]:
    """pytest-playwright's page plus a failure-only console/network sidecar.

    The Playwright trace already carries console + network, but a flat text
    file is what an agent reading ``.issue-orchestrator/diagnostics`` can
    actually grep. Events are held in memory and only written when the test
    fails — and only when the resolved artifact policy retains anything at
    all, so an operator who turned every artifact off gets no files from
    this lane either.
    """
    events: list[str] = []

    def _record(line: str) -> None:
        if len(events) < _MAX_EVENT_LINES:
            events.append(line)

    def _on_request_failed(request_: Request) -> None:
        _record(f"[requestfailed] {request_.method} {request_.url} — {request_.failure}")

    def _persist_on_failure() -> None:
        if not captures_failure_evidence(request.config.option):
            return
        # ``rep_call`` is set by pytest-playwright's own makereport hook;
        # missing means the test blew up before/around the call phase, which
        # is itself a failure worth keeping evidence for.
        report = getattr(request.node, "rep_call", None)
        if report is not None and not report.failed:
            return
        destination = browser_evidence_dir(request.node.nodeid)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "console.log").write_text(
            "\n".join(events) + "\n" if events else "(no console/network events)\n",
            encoding="utf-8",
        )
        # Printed, not raised: this is the diagnostics path for a test that
        # already failed, and it must never replace that failure.
        print(f"\n[browser-evidence] {destination}")

    page.on("console", lambda message: _record(_format_console(message)))
    page.on("pageerror", lambda error: _record(f"[pageerror] {error}"))
    page.on("requestfailed", _on_request_failed)
    try:
        yield page
    finally:
        _persist_on_failure()


@dataclass(slots=True)
class FlowWebDeps:
    """Typed subset of orchestrator deps used by dashboard browser tests."""

    timeline_store: SqliteTimelineStore
    timeline_reader: DefaultTimelineReader
    publish_recovery: MagicMock


def find_free_port() -> int:
    """Find a free localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FlowWebMockOrchestrator(MockOrchestratorForWeb):
    """Minimal orchestrator state builder for dashboard smoke tests."""

    def _create_mock_config(self) -> Config:
        """Declare a SECOND agent so agent pickers have something to pick.

        The agent list is server-owned: the dashboard template renders
        ``config.agents`` keys into ``window.dashboardData``, and every
        ``refreshViewModel`` (``dashboard/core.js``) replaces that whole
        object with a fresh server payload — on boot, on SSE open, and on
        every refresh event thereafter. A browser test that injects its own
        agent list into that global is therefore racing the app's own
        refresh: whichever lands last wins, and the loser is whichever
        banner rendered on the wrong side of it (#7140). Declaring the
        agents on the fixture repo makes the list identical on the initial
        render and on every refresh, so no ordering can change it.
        """
        config = super()._create_mock_config()
        config.agents["agent:vscode"] = AgentConfig(
            prompt_path=Path("/tmp/vscode-prompt.txt"),
            model="sonnet",
            timeout_minutes=45,
        )
        return config

    def add_queue_issue(
        self,
        issue_number: int,
        title: str,
        labels: list[str] | None = None,
    ) -> None:
        issue = Issue(
            number=issue_number,
            title=title,
            labels=labels or ["agent:web"],
        )
        self.state.cached_queue_issues.append(issue)

    def add_active_issue(
        self,
        issue_number: int,
        title: str,
        *,
        repo_root: Path,
        labels: list[str] | None = None,
    ) -> None:
        issue = Issue(
            number=issue_number,
            title=title,
            labels=labels or ["agent:web", "in-progress"],
        )
        session_name = f"issue-{issue_number}"
        worktree_path = repo_root / "worktrees" / session_name
        run_assets = FileSystemSessionOutput().start_run(
            worktree_path=worktree_path,
            session_name=session_name,
            issue_number=issue_number,
            agent_label="agent:web",
            backend="fixture",
        )
        session = Session(
            key=SessionKey(issue=FakeIssueKey(str(issue_number)), task=TaskKind.CODE),
            issue=issue,
            agent_config=AgentConfig(
                prompt_path=Path("/tmp/prompt.txt"),
                model="sonnet",
                timeout_minutes=45,
            ),
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=f"feature/issue-{issue_number}",
            started_at=datetime.now() - timedelta(minutes=7),
            run_assets=run_assets,
        )
        self.state.active_sessions.append(session)


class UvicornTestServer:
    """Manage a uvicorn server in a background thread."""

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
        raise RuntimeError(f"Uvicorn test server failed to start on {self.config.host}:{self.config.port}")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


def _seed_issue_408_timeline(store: SqliteTimelineStore, repo_root: Path) -> None:
    """Populate the smoke-test issue with a realistic coding/review lifecycle."""
    run_dir = (
        repo_root
        / ".issue-orchestrator"
        / "sessions"
        / "flow-run-1__coding-408"
    )
    _write_available_timeline_run(run_dir, issue_number=408)
    base = {
        "issue_number": 408,
        "timeline_schema_version": 4,
        "logical_run": 1,
        "logical_cycle": 1,
        "views": ["user", "ops", "debug"],
        "run_id": "flow-run-1",
        "run_dir": str(run_dir),
    }

    records = [
        TimelineRecord(
            event_id="408-session-started",
            timestamp="2026-01-01T12:00:00Z",
            event="session.started",
            source_event="session.started",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Coding session started",
                "summary": "Implement card timeline affordance",
            },
        ),
        TimelineRecord(
            event_id="408-session-completed",
            timestamp="2026-01-01T12:08:00Z",
            event="session.completed",
            source_event="session.completed",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Agent finished coding",
                "summary": "Timeline button opens issue detail drawer",
            },
        ),
        TimelineRecord(
            event_id="408-review-started",
            timestamp="2026-01-01T12:10:00Z",
            event="review.started",
            source_event="review.started",
            data={
                **base,
                "logical_phase": "review",
                "event_intent": "review",
                "agent": "agent:reviewer",
                "reviewer_agent": "agent:reviewer",
                "task": "review",
                "narrative": "Code review started",
                "summary": "Reviewer checking timeline affordance",
            },
        ),
        TimelineRecord(
            event_id="408-review-approved",
            timestamp="2026-01-01T12:14:00Z",
            event="review.approved",
            source_event="review.approved",
            data={
                **base,
                "logical_phase": "review",
                "event_intent": "review",
                "agent": "agent:reviewer",
                "reviewer_agent": "agent:reviewer",
                "task": "review",
                "narrative": "Review approved",
                "summary": "Timeline affordance verified",
            },
        ),
    ]
    for record in records:
        store.append(408, record)


def _seed_issue_409_timeline(store: SqliteTimelineStore, repo_root: Path) -> None:
    """Populate the running issue with an in-flight coding snapshot."""
    run_dir = (
        repo_root
        / ".issue-orchestrator"
        / "sessions"
        / "flow-run-2__coding-409"
    )
    _write_available_timeline_run(run_dir, issue_number=409)
    base = {
        "issue_number": 409,
        "timeline_schema_version": 4,
        "logical_run": 1,
        "logical_cycle": 1,
        "views": ["user", "ops", "debug"],
        "run_id": "flow-run-2",
        "run_dir": str(run_dir),
    }
    store.append(
        409,
        TimelineRecord(
            event_id="409-session-started",
            timestamp="2026-01-01T12:20:00Z",
            event="session.started",
            source_event="session.started",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Working on running timeline snapshot",
                "summary": "Restoring the running issue timeline snapshot on the dashboard",
            },
        ),
    )


def _write_available_timeline_run(run_dir: Path, *, issue_number: int) -> None:
    """Create a truthful available-run fixture for dashboard Timeline rows."""
    run_dir.mkdir(parents=True, exist_ok=True)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text(
        '{"event_type":"resize","offset_ms":0,"rows":24,"cols":80}\n',
        encoding="utf-8",
    )
    write_available_timeline_run_manifest(
        run_dir=run_dir,
        terminal_recording=recording,
        issue_number=issue_number,
    )


def _seed_issue_410_timeline(store: SqliteTimelineStore, repo_root: Path) -> None:
    """Populate an issue whose cycle records a ``validation.passed`` event.

    Issue 410 is the fixture for the typed ``CycleValidationBadge``
    click-through Playwright test (``test_validation_badge_click_opens_dialog``).
    It seeds a complete coding cycle plus a passed-validation event so
    the drawer renders a clickable green badge whose typed
    ``OpenValidationDetailsCommand`` dispatches into
    ``openValidationFailure`` and opens the validation dialog (issue
    #6310 AC-2 end-to-end coverage).
    """
    run_dir = (
        repo_root
        / ".issue-orchestrator"
        / "sessions"
        / "flow-run-410__coding-410"
    )
    _write_available_timeline_run(run_dir, issue_number=410)
    base = {
        "issue_number": 410,
        "timeline_schema_version": 4,
        "logical_run": 1,
        "logical_cycle": 1,
        "views": ["user", "ops", "debug"],
        "run_id": "flow-run-410",
        "run_dir": str(run_dir),
    }
    records = [
        TimelineRecord(
            event_id="410-session-started",
            timestamp="2026-01-01T13:00:00Z",
            event="session.started",
            source_event="session.started",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Coding session started",
                "summary": "Implement validated cycle fixture",
            },
        ),
        TimelineRecord(
            event_id="410-session-completed",
            timestamp="2026-01-01T13:05:00Z",
            event="session.completed",
            source_event="session.completed",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Agent finished coding",
                "summary": "Validated cycle implementation complete",
            },
        ),
        TimelineRecord(
            event_id="410-validation-passed",
            timestamp="2026-01-01T13:06:00Z",
            event="validation.passed",
            source_event="validation.passed",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Validation passed",
                "summary": "All checks green",
                "status": "completed",
            },
        ),
    ]
    for record in records:
        store.append(410, record)


def _configure_flow_deps(orchestrator: FlowWebMockOrchestrator, repo_root: Path) -> None:
    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")
    _seed_issue_408_timeline(store, repo_root)
    _seed_issue_409_timeline(store, repo_root)
    _seed_issue_410_timeline(store, repo_root)

    publish_recovery = MagicMock(spec=["can_retry_publish"])
    publish_recovery.can_retry_publish.return_value = False
    orchestrator.deps = FlowWebDeps(
        timeline_store=store,
        timeline_reader=DefaultTimelineReader(store),
        publish_recovery=publish_recovery,
    )
    orchestrator.config.repo_root = repo_root
    orchestrator.config.config_path = repo_root / ".issue-orchestrator" / "config" / "default.yaml"


@pytest.fixture(scope="module")
def web_server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Run the dashboard app with a deterministic mock orchestrator.

    Defaults to auth-off so existing flow-level Playwright tests do
    not each have to login — unit tests in ``test_web_dashboard_auth``
    already pin the middleware. One e2e test (``test_dashboard_auth``)
    flips auth on to verify the login form/redirect flow end-to-end.
    """
    orchestrator = FlowWebMockOrchestrator()
    repo_root = tmp_path_factory.mktemp("flow-dashboard-repo")
    _configure_flow_deps(orchestrator, repo_root)
    orchestrator.add_queue_issue(408, "Flow smoke item")
    orchestrator.add_queue_issue(177, "Blocked merge item", labels=["agent:web", "blocked-needs-human"])
    orchestrator.add_active_issue(409, "Running flow item", repo_root=repo_root)
    orchestrator.add_queue_issue(410, "Validated cycle fixture")
    port = find_free_port()

    # Make sure module-level auth state from an earlier test doesn't
    # leak into this run — the dashboard auth fixture below enables
    # the gate, and without this reset a module teardown race could
    # leave it on for the wider smoke suite.
    web_module.configure_dashboard_admin_token(None)

    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)

    server = UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "orchestrator": orchestrator,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)


# ---------------------------------------------------------------------------
# Browser auth helpers
#
# One login helper covers both the Control Center on port 19080 and the
# Web Dashboard on port 8080 — both surfaces share the ``/login`` form
# and session cookie model (see ``_auth_middleware``).
# ---------------------------------------------------------------------------


TEST_ADMIN_TOKEN = "test-admin-token"  # matches the unit-test fixtures


@pytest.fixture
def cc_admin_token() -> str:
    """Known admin token used by ``login_via_form``."""
    return TEST_ADMIN_TOKEN


@pytest.fixture
def authed_web_server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Run the dashboard with auth turned on.

    Mirrors ``web_server`` but calls ``configure_dashboard_admin_token``
    + ``browser_session.initialize`` before binding. Used by tests that
    need to exercise the login flow end-to-end; keeps the default
    ``web_server`` fixture auth-off so the wider smoke suite isn't
    forced to log in for every scenario.
    """
    from issue_orchestrator.infra import browser_session

    orchestrator = FlowWebMockOrchestrator()
    repo_root = tmp_path_factory.mktemp("authed-dashboard-repo")
    _configure_flow_deps(orchestrator, repo_root)
    orchestrator.add_queue_issue(408, "Flow smoke item")
    port = find_free_port()

    previous_token = web_module.get_configured_dashboard_admin_token()
    browser_session.initialize()
    web_module.configure_dashboard_admin_token(TEST_ADMIN_TOKEN)

    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)

    server = UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "orchestrator": orchestrator,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)
        web_module.configure_dashboard_admin_token(previous_token)


def login_via_form(page: Page, base_url: str, token: str) -> None:
    """Establish a browser session by submitting the login form.

    Works against the Control Center (19080) and the Web Dashboard
    (8080); both share the form shape. Short-circuits when the
    target has auth disabled (``GET /`` returns the app directly).
    """
    page.goto(f"{base_url}/")
    if "Sign in" not in page.content():
        return
    page.fill('input[name="token"]', token)
    page.click('button[type="submit"]')
    # The login handler responds 303 → /. Wait for the redirected
    # URL to confirm the session cookie took effect, and that we're
    # no longer staring at the login form.
    page.wait_for_url(f"{base_url}/", timeout=5000)
