"""Shared fixtures and configuration for tests."""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import pytest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, PropertyMock, patch
from fastapi.testclient import TestClient
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.infra.config import Config, DangerousConfig
from issue_orchestrator.infra.hooks.hookspec import hookimpl
from issue_orchestrator.ports.pull_request_tracker import (
    MergeQueueEntry,
    MergeQueueRead,
    PRInfo,
    PRRef,
    StatusCheckRollupRead,
)
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot
from issue_orchestrator.domain.issue_key import FakeIssueKey, IssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

TEST_ADMIN_TOKEN = "test-admin-token"
TEST_AGENT_CALLBACK_TOKEN = "test-agent-callback-token"


# =============================================================================
# Git Environment Isolation
# =============================================================================
# Prevent tests from accidentally writing git config to the main repo.
# When GIT_DIR is set, `git config` writes to that repo regardless of cwd.

@pytest.fixture(autouse=True)
def isolate_git_env(monkeypatch):
    """Strip git env vars to prevent test git commands from affecting main repo.

    This is critical because:
    - Tests create temp repos and run `git config user.email test@test.com`
    - If GIT_DIR is set in the environment, that config goes to the MAIN repo
    - This causes pollution like user.name="Test User" in the real repo

    This fixture runs for EVERY test (autouse=True) and strips these vars.
    """
    git_env_vars = [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ]
    for var in git_env_vars:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolate_orchestrator_env(monkeypatch, tmp_path):
    """Strip orchestrator env vars and set safe defaults.

    When the orchestrator launches an agent, it exports ISSUE_ORCHESTRATOR_*
    vars (SESSION_ID, CONFIG_PATH, etc.) for coding-done/reviewer-done — and
    their legacy unprefixed ORCHESTRATOR_* fallbacks (ORCHESTRATOR_SESSION_ID,
    ORCHESTRATOR_CONFIG_NAME/PATH, ORCHESTRATOR_API_PORT, ...). If the agent then
    runs `make validate-quick` (pytest), these vars leak into unit tests and cause
    failures — e.g., CONFIG_PATH overrides test-local configs, SESSION_ID forces
    managed mode over mocked values, and a leaked ORCHESTRATOR_SESSION_ID breaks
    TestAgentGateIntegration when tests mock ``get_session_id`` without injecting
    a RUN_DIR. Code reads the legacy unprefixed names as fallbacks
    (get_session_id, preflight push), so stripping only the ISSUE_-prefixed names
    is not enough when the orchestrator self-hosts.

    Similarly, ORCHESTRATOR_WORKTREE_BASE_BRANCH (set by e2e fixtures) can
    override test expectations in resolve_base_branch tests; it is covered by the
    ORCHESTRATOR_ prefix sweep below.

    This fixture strips both prefixes so tests always start with a clean env,
    then sets ISSUE_ORCHESTRATOR_REPO_ROOT to a temp directory so any code that
    resolves repo_root from the environment (e.g. SubprocessPlugin) never
    accidentally targets the real repo.  Tests that need a specific repo_root
    override this with their own ``monkeypatch.setenv()``.
    Both env-var families are stripped. Several runtime readers accept a legacy
    non-prefixed ``ORCHESTRATOR_*`` form as a fallback to the canonical
    ``ISSUE_ORCHESTRATOR_*`` one (e.g. config resolution reads
    ``ORCHESTRATOR_CONFIG_PATH`` / ``ORCHESTRATOR_CONFIG_NAME`` and managed-mode
    detection reads ``ORCHESTRATOR_SESSION_ID``). Stripping only the prefixed
    form left the legacy form leaking into the suite when the agent runs
    ``make validate-quick`` inside an orchestrator session, so both prefixes are
    cleared here.
    """
    # Strip every orchestrator-injected var so tests start from a clean env.
    # The orchestrator exports ISSUE_ORCHESTRATOR_* and also a few bare, legacy
    # ORCHESTRATOR_* forms — e.g. the persistent review-exchange runner sets
    # ORCHESTRATOR_SESSION_ID, which coding-done/_is_managed_session still accept
    # for compatibility. Leaving the bare forms in place makes every test look
    # like a managed orchestrator session and breaks standalone-path tests, so
    # strip both prefixes (this subsumes ORCHESTRATOR_WORKTREE_BASE_BRANCH). The
    # two prefixes are disjoint ("ISSUE_ORCHESTRATOR_" does not start with
    # "ORCHESTRATOR_"), so each var is handled once.
    for var in list(os.environ):
        if var.startswith("ISSUE_ORCHESTRATOR_") or var.startswith("ORCHESTRATOR_"):
            monkeypatch.delenv(var, raising=False)

    # Set a safe default REPO_ROOT so SubprocessPlugin (and anything else
    # that reads this env var) never falls back to Path.cwd() (the base repo).
    # This prevents tests from accidentally opening session_registry.sqlite
    # in the real repo's state dir.
    safe_repo = tmp_path / "isolated-repo-root"
    safe_repo.mkdir(exist_ok=True)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(safe_repo))


@pytest.fixture(autouse=True)
def reset_control_api_token():
    """Reset process-wide browser auth and bearer-token enforcement.

    ``ControlAPIServer.start`` (and tests that explicitly call
    ``configure_api_token``) install process-wide tokens on the
    ``control_app`` module. Dashboard auth has a separate process-wide
    token. Without an autouse reset, leftover tokens from an earlier
    test cause unrelated TestClient calls to return 401 instead of the
    expected status. See security issue #5987 (F3) + #6017 P3.
    """
    try:
        from issue_orchestrator.entrypoints.control_api import configure_api_token
        from issue_orchestrator.entrypoints.web import configure_dashboard_admin_token
        from issue_orchestrator.infra import browser_session
    except Exception:
        yield
        return
    configure_api_token(None, agent_callback=None)
    configure_dashboard_admin_token(None)
    browser_session.shutdown()
    try:
        yield
    finally:
        configure_api_token(None, agent_callback=None)
        configure_dashboard_admin_token(None)
        browser_session.shutdown()


@dataclass(frozen=True)
class FakeBrowserAuth:
    """Deterministic test auth that exercises the real browser middleware.

    This is the "semi-enabled" mode for route/UI tests: no real token
    file and no manual operator login, but requests still pass through
    bearer-token, session-cookie, CSRF, and SSE-token checks.
    """

    admin_token: str = TEST_ADMIN_TOKEN
    agent_callback_token: str = TEST_AGENT_CALLBACK_TOKEN

    def login(self, client: TestClient) -> str:
        """Log the TestClient in and return the session's CSRF token."""
        from issue_orchestrator.infra import browser_session

        response = client.post("/login", json={"token": self.admin_token})
        assert response.status_code == 200, response.text
        session_id = client.cookies.get(browser_session.SESSION_COOKIE)
        assert session_id, "login did not set browser session cookie"
        csrf = browser_session.get_csrf_token(session_id)
        assert csrf, "login session did not yield a CSRF token"
        return csrf

    def csrf_headers(self, client: TestClient) -> dict[str, str]:
        """Return the X-CSRF-Token header for a logged-in TestClient."""
        from issue_orchestrator.infra import browser_session

        session_id = client.cookies.get(browser_session.SESSION_COOKIE)
        assert session_id, "client must be logged in before requesting CSRF headers"
        csrf = browser_session.get_csrf_token(session_id)
        assert csrf, "logged-in client did not yield a CSRF token"
        return {browser_session.CSRF_HEADER: csrf}

    def bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}


@pytest.fixture
def fake_browser_auth() -> FakeBrowserAuth:
    """Enable deterministic browser auth across Control API and Dashboard.

    Use this for tests that should catch auth wiring regressions without
    depending on ``~/.issue-orchestrator/api-token``.
    """
    from issue_orchestrator.entrypoints.control_api import configure_api_token
    from issue_orchestrator.entrypoints.web import configure_dashboard_admin_token
    from issue_orchestrator.infra import browser_session

    browser_session.shutdown()
    browser_session.initialize(admin_token=TEST_ADMIN_TOKEN)
    configure_api_token(TEST_ADMIN_TOKEN, agent_callback=TEST_AGENT_CALLBACK_TOKEN)
    configure_dashboard_admin_token(TEST_ADMIN_TOKEN)
    return FakeBrowserAuth()


@pytest.fixture
def auth_enabled_control_client(fake_browser_auth: FakeBrowserAuth) -> TestClient:
    """Control API TestClient with auth enabled but no browser login."""
    from issue_orchestrator.entrypoints.control_api import control_app

    return TestClient(control_app)


@pytest.fixture
def auth_enabled_dashboard_client(fake_browser_auth: FakeBrowserAuth) -> TestClient:
    """Dashboard TestClient with auth enabled but no browser login."""
    from issue_orchestrator.entrypoints.web import app

    return TestClient(app)


@pytest.fixture
def logged_in_dashboard_client(
    auth_enabled_dashboard_client: TestClient,
    fake_browser_auth: FakeBrowserAuth,
) -> TestClient:
    """Dashboard TestClient with auth enabled and a valid browser session."""
    fake_browser_auth.login(auth_enabled_dashboard_client)
    return auth_enabled_dashboard_client


class MockGitHubAdapter:
    """Mock GitHub adapter implementing port interfaces for testing.

    This is the proper way to test hexagonal architecture - inject a mock
    adapter rather than patching individual functions.
    """

    def __init__(self):
        # Storage for test data
        self.issues: list[Issue] = []
        self.labels: dict[int, set[str]] = {}  # issue_number -> labels
        self.prs: dict[str, list[PRInfo]] = {}  # branch -> PRs
        self.comments: list[dict] = []
        # (issue_number, ISO-8601Z timestamp) per close, mirroring GitHub's
        # /issues/{n}/events "closed" entries for issue_closed_on_or_after.
        # Timestamps are DETERMINISTIC (tests/AGENTS.md: never the real
        # clock): a fixed far-future base plus a per-event counter, so any
        # close recorded during a scenario sorts after any fixture merged_at.
        # Tests needing precise ordering seed issue_close_events directly.
        self.issue_close_events: list[tuple[int, str]] = []
        self._close_event_seq = 0
        self.pr_reviews: dict[int, list[dict]] = {}  # pr_number -> reviews
        self.close_pr_calls: list[int] = []
        # pr_number -> current merge queue entry (None/absent = not enqueued)
        self.merge_queue_entries: dict[int, "MergeQueueEntry"] = {}

        # Call tracking for assertions
        self.add_label_calls: list[tuple] = []
        self.remove_label_calls: list[tuple] = []
        self.list_issues_calls: list[dict] = []
        self.get_prs_calls: list[dict] = []
        self.search_pr_refs_calls: list[int] = []
        self.enqueue_merge_queue_calls: list[int] = []

    # IssueRepository methods
    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
        *,
        exhaustive: bool = False,
    ) -> list[Issue]:
        """Return configured test issues, filtered by labels."""
        self.list_issues_calls.append({
            "labels": labels, "milestone": milestone, "state": state, "limit": limit,
            "required_stable_ids": required_stable_ids, "exhaustive": exhaustive,
        })
        result = self.issues
        if labels:
            result = [i for i in result if any(l in i.labels for l in labels)]
        return result[:limit]

    def list_issues_delta(
        self,
        *,
        since: str,
        limit: int = 100,
    ) -> tuple[list[Issue], str | None]:
        """Return issues updated since watermark (mock implementation)."""
        return self.issues[:limit], None

    def get_issue(self, issue_number: int) -> Optional[Issue]:
        """Get a specific issue."""
        for issue in self.issues:
            if issue.number == issue_number:
                return issue
        return None

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue."""
        issue = self.get_issue(issue_number)
        return issue.state if issue else None

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        """Get dependency-gating issue facts."""
        issue = self.get_issue(issue_number)
        if issue is None:
            return None
        return DependencyIssueSnapshot(state=issue.state, milestone=issue.milestone)

    def create_issue_key(self, issue_number: int) -> IssueKey:
        """Create an IssueKey for testing."""
        return FakeIssueKey(name=str(issue_number))

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get labels for an issue."""
        return sorted(self.labels.get(issue_number, set()))

    def get_issue_labels_fresh(self, issue_number: int) -> list[str]:
        """Get fresh labels for an issue."""
        return self.get_issue_labels(issue_number)

    # LabelManager methods
    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue."""
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)
        issue = self.get_issue(issue_number)
        if issue is not None and label not in issue.labels:
            issue.labels.append(label)
        pr = self.get_pr(issue_number)
        if pr and label not in pr.labels:
            pr.labels.append(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue."""
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)
        issue = self.get_issue(issue_number)
        if issue is not None and label in issue.labels:
            issue.labels.remove(label)
        pr = self.get_pr(issue_number)
        if pr and label in pr.labels:
            pr.labels.remove(label)

    def has_label(self, issue_number: int, label: str) -> bool:
        """Check if issue has a specific label."""
        return label in self.labels.get(issue_number, set())

    # PRRepository methods
    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get PRs for a branch."""
        self.get_prs_calls.append({"branch": branch, "state": state})
        return self.prs.get(branch, [])

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get PRs with a specific label."""
        result = []
        for prs in self.prs.values():
            for pr in prs:
                if label in pr.labels:
                    result.append(pr)
        return result

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get PRs for an issue by matching branch prefix or title."""
        prefix = f"{issue_number}-"
        result: list[PRInfo] = []
        for branch, prs in self.prs.items():
            for pr in prs:
                if state != "all" and pr.state.lower() != state.lower():
                    continue
                if branch.startswith(prefix) or f"#{issue_number}" in pr.title:
                    result.append(pr)
        return result

    def search_pr_refs_for_issue(self, issue_number: int) -> list[PRRef]:
        """Lightweight refs for an issue (any state), mirroring the real
        adapter's search-only lookup — no per-PR hydration."""
        self.search_pr_refs_calls.append(issue_number)
        return [
            PRRef(number=pr.number, url=pr.url, title=pr.title, body=pr.body)
            for pr in self.get_prs_for_issue(issue_number, state="all")
        ]

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List all PRs."""
        result = []
        for prs in self.prs.values():
            for pr in prs:
                if state == "all" or pr.state.lower() == state.lower():
                    result.append(pr)
        return result[:limit]

    def get_pr(self, pr_number: int) -> Optional[PRInfo]:
        """Get a specific PR."""
        for prs in self.prs.values():
            for pr in prs:
                if pr.number == pr_number:
                    return pr
        return None

    def read_pr_status_check_rollup(
        self, pr_number: int, *, skip_primary_source: bool = False
    ) -> StatusCheckRollupRead:
        """Read a PR's status-check rollup. The mock has no check-status
        concept, so it reports ``ok`` with whatever ``status_check_rollup``
        the test fixture set on the PRInfo (defaults to None). The real
        adapter pays a GraphQL round-trip and can report permission/
        transient failures; the mock is free and always-capable.

        ``skip_primary_source`` is accepted for protocol parity (the gate
        passes it during a GraphQL backoff window); the mock has a single
        always-readable source, so it behaves identically either way."""
        pr = self.get_pr(pr_number)
        state = pr.status_check_rollup if pr is not None else None
        return StatusCheckRollupRead(state=state, capability="ok")

    def enqueue_to_merge_queue(self, pr_number: int) -> None:
        """Record an enqueue and mark the PR as QUEUED (mock)."""
        self.enqueue_merge_queue_calls.append(pr_number)
        self.merge_queue_entries[pr_number] = MergeQueueEntry(state="QUEUED")

    def read_merge_queue_entry(self, pr_number: int) -> MergeQueueRead:
        """Return the PR's typed merge queue read (mock).

        PRESENT when an entry was recorded, ABSENT otherwise. Tests that need the
        INDETERMINATE (unreadable/unmodeled) outcome inject it directly.
        """
        entry = self.merge_queue_entries.get(pr_number)
        return (
            MergeQueueRead.present(entry)
            if entry is not None
            else MergeQueueRead.absent()
        )

    def create_pr(self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None) -> PRInfo:
        """Create a new PR (mock)."""
        pr = PRInfo(
            number=100,
            title=title,
            url=f"https://github.com/test/repo/pull/100",
            branch=head,
            body=body,
            state="open",
            labels=[],
            draft=draft,
        )
        self.prs.setdefault(head, []).append(pr)
        return pr

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment (mock)."""
        self.comments.append({"number": issue_or_pr_number, "body": body})
        return f"https://github.com/test/repo/issues/{issue_or_pr_number}#comment"

    def get_pr_reviews(self, pr_number: int) -> list[dict]:
        """Get reviews for a PR (mock)."""
        return self.pr_reviews.get(pr_number, [])

    def close_pr(self, pr_number: int) -> None:
        """Close a PR (mock)."""
        self.close_pr_calls.append(pr_number)
        pr = self.get_pr(pr_number)
        if pr is not None:
            pr.state = "closed"

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict | None:
        """Create a new issue (mock).

        Returns a dict with issue data (number, html_url).
        """
        # Generate next issue number
        existing_numbers = [i.number for i in self.issues]
        next_number = max(existing_numbers) + 1 if existing_numbers else 1

        issue = Issue(
            number=next_number,
            title=title,
            body=body,
            labels=labels or [],
            milestone_number=milestone,
        )
        self.issues.append(issue)
        return {"number": next_number, "html_url": f"https://github.com/test/repo/issues/{next_number}"}

    def list_milestones(self, state: str = "open") -> list[dict]:
        return getattr(self, "milestones", [])

    def create_milestone(
        self,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
        state: str = "open",
    ) -> dict | None:
        milestones = getattr(self, "milestones", [])
        number = len(milestones) + 1
        milestone = {
            "number": number,
            "title": title,
            "description": description,
            "due_on": due_on,
            "state": state,
        }
        milestones.append(milestone)
        self.milestones = milestones
        return milestone

    def update_issue_milestone(self, issue_number: int, milestone: int | None) -> None:
        issue = self.get_issue(issue_number)
        if issue is None:
            return
        issue.milestone_number = milestone
        if milestone is None:
            issue.milestone = None
            return
        for entry in getattr(self, "milestones", []):
            if entry.get("number") == milestone:
                issue.milestone = entry.get("title")
                return

    def update_issue_state(self, issue_number: int, state: str) -> None:
        issue = self.get_issue(issue_number)
        if issue is not None:
            issue.state = state
            if state == "closed":
                self._close_event_seq += 1
                seq = self._close_event_seq
                self.issue_close_events.append((
                    issue_number,
                    f"9999-01-01T00:{seq // 60:02d}:{seq % 60:02d}Z",
                ))

    def issue_closed_on_or_after(self, issue_number: int, timestamp: str) -> bool:
        """Whether a recorded close event exists at/after ``timestamp`` (mock).

        Mirrors the GitHub adapter's typed close-event evidence read. The mock
        does not simulate GitHub's closing-keyword auto-close, so scenarios
        that merge a PR without closing its issue genuinely have no close
        event — the close-on-merge fallback then behaves exactly as it would
        against real GitHub.
        """
        return any(
            number == issue_number and closed_at >= timestamp
            for number, closed_at in self.issue_close_events
        )


@pytest.fixture
def mock_repository_host():
    """Create a mock GitHub adapter for testing."""
    return MockGitHubAdapter()


class MockTerminalPlugin:
    """Mock terminal plugin for testing.

    Implements terminal hooks and tracks calls for test assertions.
    """

    def __init__(self):
        self.sessions: dict[int, dict] = {}
        self.create_session_calls = []
        self.session_exists_calls = []
        self.kill_session_calls = []
        # Control behavior for tests
        self.session_exists_override = None  # Set to True/False to override

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        """Track session creation."""
        self.create_session_calls.append({
            "session_id": session_id,
            "session_name": session_name,
            "command": command,
            "working_dir": working_dir,
            "title": title,
        })
        self.sessions[session_id] = {
            "command": command,
            "working_dir": working_dir,
            "title": title,
            "session_name": session_name,
        }
        return True

    @hookimpl
    def session_exists(self, session_id: int, session_name: str) -> bool:
        """Check if session was created."""
        self.session_exists_calls.append((session_id, session_name))
        if self.session_exists_override is not None:
            return self.session_exists_override
        return session_id in self.sessions

    @hookimpl
    def kill_session(self, session_id: int, session_name: str) -> bool:
        """Remove session."""
        self.kill_session_calls.append((session_id, session_name))
        self.sessions.pop(session_id, None)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict]:
        """Return empty list for tests."""
        return []

    @hookimpl
    def cleanup_idle_sessions(self) -> int:
        """Return 0 for tests."""
        return 0

    @hookimpl
    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        """Return None for tests."""
        return None


class MockPluginManager:
    """Mock plugin manager for testing.

    Wraps MockTerminalPlugin with the same interface as PluginManager.
    """

    def __init__(self, plugin: MockTerminalPlugin | None = None):
        self._plugin = plugin or MockTerminalPlugin()

    @property
    def plugin(self) -> MockTerminalPlugin:
        """Access the underlying mock plugin for assertions."""
        return self._plugin

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        return self._plugin.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
            session_name=session_name,
        )

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return self._plugin.session_exists(session_id=session_id, session_name=session_name)

    def kill_session(self, session_id: int, session_name: str) -> None:
        self._plugin.kill_session(session_id=session_id, session_name=session_name)

    def discover_running_sessions(self) -> list[dict]:
        return self._plugin.discover_running_sessions()

    def cleanup_idle_sessions(self) -> int:
        return self._plugin.cleanup_idle_sessions()

    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        return self._plugin.get_session_output(session_id=session_id, lines=lines, session_name=session_name)

    def emit(self, event: str, data: dict | None = None) -> None:
        """Emit a trace event (no-op for mock, but tracks calls)."""
        if not hasattr(self, '_emit_calls'):
            self._emit_calls = []
        self._emit_calls.append((event, data or {}))


class MockEventSink:
    """Mock EventSink for testing.

    Implements the EventSink protocol and tracks published events.
    """

    def __init__(self):
        self.events: list = []

    def publish(self, event) -> None:
        """Record the event for test assertions."""
        self.events.append(event)

    def get_events_by_name(self, name: str) -> list:
        """Get all events with the given name."""
        return [e for e in self.events if e.name == name]

    def clear(self) -> None:
        """Clear recorded events."""
        self.events.clear()


class MockSessionRunner:
    """Mock SessionRunner for testing.

    Implements the SessionRunner protocol and tracks calls.
    """

    def __init__(self, plugin: MockTerminalPlugin | None = None):
        self._plugin = plugin or MockTerminalPlugin()

    @property
    def plugin(self) -> MockTerminalPlugin:
        """Access the underlying mock plugin for assertions."""
        return self._plugin

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        return self._plugin.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
            session_name=session_name,
        )

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return self._plugin.session_exists(session_id=session_id, session_name=session_name)

    def kill_session(self, session_id: int, session_name: str) -> None:
        self._plugin.kill_session(session_id=session_id, session_name=session_name)

    def discover_running_sessions(self) -> list[dict]:
        return self._plugin.discover_running_sessions()

    def cleanup_idle_sessions(self) -> int:
        return self._plugin.cleanup_idle_sessions()

    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        return self._plugin.get_session_output(session_id=session_id, lines=lines, session_name=session_name)

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        """No-op for mock."""
        pass

    def on_orchestrator_shutdown(self) -> None:
        """No-op for mock."""
        pass


@pytest.fixture
def mock_terminal_plugin():
    """Create a mock terminal plugin for testing."""
    return MockTerminalPlugin()


@pytest.fixture
def mock_plugin_manager(mock_terminal_plugin):
    """Create a mock plugin manager for testing (legacy, use mock_session_runner)."""
    return MockPluginManager(mock_terminal_plugin)


@pytest.fixture
def mock_event_sink():
    """Create a mock EventSink for testing."""
    return MockEventSink()


@pytest.fixture
def mock_session_runner(mock_terminal_plugin):
    """Create a mock SessionRunner for testing."""
    return MockSessionRunner(mock_terminal_plugin)


def _build_null_queue_cache_store():
    """Create a QueueCacheStore mock that returns empty data (cold start)."""
    store = MagicMock()
    store.load_issues.return_value = []
    store.load_watermark.return_value = None
    return store


def build_test_orchestrator_deps(
    config,
    repo_host,
    events,
    runner,
    worktree_manager,
    working_copy=None,
    *,
    session_controller=None,
    label_sync=None,
    fact_gatherer=None,
    planner=None,
    session_manager=None,
    action_applier=None,
    claim_manager=None,
    lease_renewer=None,
    timeline_reader=None,
    timeline_writer=None,
    provider_readiness_probe=None,
):
    """Factory function to create OrchestratorDeps for testing.

    This creates properly wired control components with injected mocks,
    enabling explicit dependency injection. Returns an OrchestratorDeps
    frozen dataclass (no nulls, no optionals).

    Args:
        config: Config object
        repo_host: Repository host (MockGitHubAdapter or similar)
        events: EventSink (MockEventSink or similar)
        runner: SessionRunner (MockSessionRunner or similar)
        worktree_manager: WorktreeManager mock
        working_copy: Optional WorkingCopy (defaults to GitWorkingCopy)
        session_controller: Optional override for SessionController (for testing)
        label_sync: Optional override for LabelSync (for testing)
        fact_gatherer: Optional override for FactGatherer (for testing)
        planner: Optional override for Planner (for testing)
        session_manager: Optional override for SessionManager (for testing)
        action_applier: Optional override for ActionApplier (for testing)
        provider_readiness_probe: Optional ProviderReadinessProbe. Defaults to
            the explicit "nothing to probe" reader, so a test that does not
            care about provider credentials never spawns a CLI probe and never
            silently claims a provider is authenticated (#6999).

    Returns:
        OrchestratorDeps with all components wired
    """
    from issue_orchestrator.control.scheduler import Scheduler
    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.session_manager import SessionManager
    from issue_orchestrator.control.action_applier import ActionApplier
    from issue_orchestrator.control.fact_gatherer import FactGatherer
    from issue_orchestrator.control.state_machine_manager import StateMachineManager
    from issue_orchestrator.control.session_controller import SessionController
    from issue_orchestrator.control.completion_processor import CompletionProcessor
    from issue_orchestrator.control.pr_scanner import PRScanner
    from issue_orchestrator.control.health_gate import HealthGate
    from issue_orchestrator.control.session_restorer import SessionRestorer
    from issue_orchestrator.control.completion_dispatcher import SynchronousCompletionDispatcher
    from datetime import datetime

    from issue_orchestrator.control.label_sync import LabelSync
    from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
    from issue_orchestrator.control.orchestrator_deps import OrchestratorDeps
    from issue_orchestrator.entrypoints.bootstrap_completion import (
        build_completion_handler_factory,
    )
    from issue_orchestrator.entrypoints.bootstrap_session_launcher import (
        build_session_launcher_factory,
    )
    from tests.callback_endpoint_helpers import ready_callback_endpoint
    from issue_orchestrator.events import EventHub
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
    from issue_orchestrator.execution.command_runner import LocalCommandRunner
    from unittest.mock import MagicMock, AsyncMock

    if working_copy is None:
        working_copy = GitWorkingCopy()
    command_runner = LocalCommandRunner()

    # Create control components with injected mocks
    # Use provided overrides or create defaults
    scheduler = Scheduler(config=config)
    _planner = planner or Planner(config=config, scheduler=scheduler)
    _session_manager = session_manager or SessionManager(runner=runner, events=events, config=config)
    _fact_gatherer = fact_gatherer or FactGatherer(config=config, repository_host=repo_host, events=events)
    state_machine_manager = StateMachineManager(config=config)

    session_output = FileSystemSessionOutput()
    from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
        InMemoryPersistentExchangePairRegistry,
    )
    from issue_orchestrator.execution.persistent_review_exchange_runner import (
        PersistentReviewExchangeRunner,
    )
    agent_callback_endpoint = ready_callback_endpoint()

    completion_processor = CompletionProcessor(
        agent_callback_endpoint=agent_callback_endpoint,
        label_adapter=repo_host,
        pr_adapter=repo_host,
        git_adapter=working_copy,
        event_bus=None,
        session_output=session_output,
        review_exchange_runner=PersistentReviewExchangeRunner(
            session_output, InMemoryPersistentExchangePairRegistry(),
        ),
        label_config={
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "code_reviewed": config.code_reviewed_label or "code-reviewed",
            "needs_rework": config.get_label_needs_rework(),
            "code_review": config.code_review_label or "needs-code-review",
            "in_progress": config.get_label_in_progress(),
        },
        config=config,
    )
    _session_controller = session_controller or SessionController(
        completion_processor=completion_processor,
        events=events,
        session_output=session_output,
        working_copy=working_copy,
        command_runner=command_runner if config.validation.quick.cmd else None,
        validation_cmd=config.validation.quick.cmd,
        validation_timeout_seconds=config.validation.quick.timeout_seconds,
        max_validation_retries=config.retry.max_validation_retries,
    )
    pr_scanner = PRScanner(
        config=config,
        repository=repo_host,
        events=events,
    )
    fresh_reader = MagicMock()
    fresh_reader.read_issue_labels.return_value = []
    e2e_issue_tracker = MagicMock()

    _action_applier = action_applier or ActionApplier(
        labels=repo_host,
        sessions=_session_manager,
        events=events,
        repository_host=repo_host,
        worktree_manager=worktree_manager,
        fresh_issue_reader=fresh_reader,
        reconcile=False,
    )

    health_gate = HealthGate(rate_limit_threshold=100)

    session_restorer = SessionRestorer(
        config=config,
        repository_host=repo_host,
        working_copy=working_copy,
    )

    _label_sync = label_sync or LabelSync(labels=repo_host, events=events, pr_tracker=repo_host)
    event_hub = EventHub()
    # Create manifest downloader for tech_lead sessions
    from issue_orchestrator.execution.tech_lead_downloader import TechLeadDownloader
    manifest_downloader = TechLeadDownloader(
        repository_host=repo_host,
        command_runner=command_runner,
    )

    # Create claim components (NullClaimManager by default for tests)
    from issue_orchestrator.ports.claim_manager import NullClaimManager
    from issue_orchestrator.domain.lease_config import LeaseConfig
    from issue_orchestrator.control.claim_gate import ClaimGate
    from issue_orchestrator.control.lease_renewer import LeaseRenewer

    lease_config = LeaseConfig.for_testing()
    claim_manager = claim_manager or NullClaimManager()
    claim_gate = ClaimGate(claim_manager=claim_manager, events=events)
    lease_renewer = lease_renewer or LeaseRenewer(
        claim_manager=claim_manager,
        events=events,
        config=lease_config,
    )

    from issue_orchestrator.execution.goal_pilot_store import SqliteGoalPilotStore
    goal_pilot_store = SqliteGoalPilotStore(repo_root=config.repo_root)
    from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
    from issue_orchestrator.ports import (
        InMemoryProviderCircuitStore,
        NullTimelineReader,
        NullTimelineStore,
        NullTimelineWriter,
    )
    provider_resilience = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=events,
    )
    timeline_reader = timeline_reader or NullTimelineReader()
    timeline_writer = timeline_writer or NullTimelineWriter()

    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.control.infra_services import InfraServices
    from issue_orchestrator.control.publish_recovery import PublishRecoveryService
    from issue_orchestrator.entrypoints.bootstrap import create_attempt_store
    from issue_orchestrator.execution.label_store import LabelStore

    label_manager = LabelManager(config)
    label_store = LabelStore(config.repo_root / ".issue-orchestrator" / "label_store.sqlite")
    attempt_store = create_attempt_store(config)
    from issue_orchestrator.infra.tech_lead_authority_store import (
        SqliteTechLeadAuthorityStore,
    )
    tech_lead_authority = SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
    from issue_orchestrator.infra.open_issue_corpus_store import (
        SqliteOpenIssueCorpusStore,
    )
    open_issue_corpus_store = SqliteOpenIssueCorpusStore.for_repo(config.repo_root)
    from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
    open_issue_corpus = OpenIssueCorpusManager(
        repo_host,
        open_issue_corpus_store,
        is_enabled=lambda: bool(
            config.tech_lead_review_agent and config.tech_lead.dedup.enabled
        ),
    )

    _action_applier.claim_gate = claim_gate
    # build_test_orchestrator_deps() returns deps without a live Orchestrator state.
    # Use an explicit no-op lease lookup so claim verification behavior is predictable
    # for tests that consume deps directly instead of relying on runtime wiring.
    _action_applier.lease_id_lookup = lambda _issue_number: None

    from issue_orchestrator.ports.provider_readiness import (
        NO_PROVIDER_READINESS_PROBE,
    )

    readiness_probe = provider_readiness_probe or NO_PROVIDER_READINESS_PROBE

    infra_services = InfraServices(
        label_manager=label_manager,
        label_store=label_store,
        queue_cache_store=_build_null_queue_cache_store(),
        provider_resilience=provider_resilience,
        provider_readiness_probe=readiness_probe,
        timeline_reader=timeline_reader,
        timeline_store=NullTimelineStore(),
        timeline_writer=timeline_writer,
        goal_pilot_store=goal_pilot_store,
        attempt_store=attempt_store,
        tech_lead_authority=tech_lead_authority,
        open_issue_corpus=open_issue_corpus,
    )

    from issue_orchestrator.execution.json_publish_retry_locator_store import (
        JsonPublishRetryLocatorStore,
    )
    from issue_orchestrator.execution.thread_background_job_runner import (
        ThreadBackgroundJobRunner,
    )
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    publish_recovery = PublishRecoveryService(
        repository_host=repo_host,
        completion_processor=completion_processor,
        locator_store=JsonPublishRetryLocatorStore(
            config.repo_root / ".issue-orchestrator" / "state" / "publish_retry_locators.json"
        ),
        runner=ThreadBackgroundJobRunner(),
        label_manager=label_manager,
        fresh_issue_reader=fresh_reader,
        # Use the resolved applier (same object OrchestratorDeps exposes), not
        # the raw optional argument which is None when callers omit it.
        action_applier=_action_applier,
        code_review_agent_configured=bool(config.code_review_agent),
        tech_lead_authority=tech_lead_authority,
    )
    # Same post-construction wiring as bootstrap: the ActionApplier abandons
    # publish retries at issue terminal boundaries via the runtime terminator.
    _action_applier.publish_recovery = publish_recovery

    return OrchestratorDeps(
        events=events,
        runner=runner,
        # Orchestrator-owned, outside every worktree, exactly as bootstrap
        # wires it (#6999 F7).
        pending_work_claims=SqlitePendingWorkClaimStore.for_repo(config.repo_root),
        # The same endpoint the completion processor got, mirroring how
        # bootstrap shares one instance. Nothing binds a port in tests, so
        # it honestly resolves to "no endpoint yet".
        agent_callback_endpoint=agent_callback_endpoint,
        # Same shape as bootstrap: assembly at the composition boundary,
        # closing over these collaborators (#6924 A3-R2).
        session_launcher_factory=build_session_launcher_factory(
            config=config,
            events=events,
            repository_host=repo_host,
            action_applier=_action_applier,
            session_manager=_session_manager,
            worktree_manager=worktree_manager,
            working_copy=working_copy,
            command_runner=command_runner,
            session_output=session_output,
            manifest_downloader=manifest_downloader,
            tech_lead_authority=tech_lead_authority,
            claim_manager=claim_manager,
            provider_resilience=provider_resilience,
            state_machine_manager=state_machine_manager,
            label_manager=label_manager,
            agent_callback_endpoint=agent_callback_endpoint,
            provider_readiness_probe=readiness_probe,
        ),
        # Same shape again for the completion handler (#6999 A4).
        completion_handler_factory=build_completion_handler_factory(
            config,
            events=events,
            repository_host=repo_host,
            session_output=session_output,
            tech_lead_authority=tech_lead_authority,
            open_issue_corpus=open_issue_corpus,
            label_manager=label_manager,
            provider_resilience=provider_resilience,
        ),
        repository_host=repo_host,
        e2e_issue_tracker=e2e_issue_tracker,
        fresh_issue_reader=fresh_reader,
        event_hub=event_hub,
        planner=_planner,
        session_manager=_session_manager,
        label_sync=_label_sync,
        action_applier=_action_applier,
        fact_gatherer=_fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        command_runner=command_runner,
        manifest_downloader=manifest_downloader,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=_session_controller,
        completion_dispatcher=SynchronousCompletionDispatcher(),
        health_gate=health_gate,
        # Null fact sources: unit tests exercising snapshots inject their own.
        board_snapshot_builder=BoardSnapshotBuilder(
            timeline_reader=lambda issue, limit: [],
            log_tail_provider=lambda lines: [],
            case_file_reader=lambda: (),
            shipped_fix_reader=lambda limit: (),
            e2e_health_reader=lambda now: None,
            session_activity_reader=lambda session: None,
            clock=datetime.now,
        ),
        session_output=session_output,
        claim_manager=claim_manager,
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        publish_recovery=publish_recovery,
        services=infra_services,
    )


# NOTE: The autouse patch_orchestrator_dependencies fixture has been removed.
# Tests now use explicit DI via create_test_orchestrator() or build_test_orchestrator_deps().
# See tests/unit/test_orchestrator.py::create_test_orchestrator() for the pattern.
#
# If you need the legacy patching behavior for backward compatibility, use:
#   @pytest.fixture
#   def patched_orchestrator(monkeypatch):
#       # ... patching code here ...


@pytest.fixture
def patch_plugin_manager():
    """Create a MockSessionRunner for test assertions.

    DEPRECATED: Prefer using create_test_orchestrator(runner=runner) instead.
    This fixture exists for backward compatibility with tests that expect
    patch_plugin_manager.plugin to be the shared plugin instance.
    """
    return MockSessionRunner()


@pytest.fixture
def explicit_orchestrator_deps(request):
    """Create Orchestrator with all dependencies explicitly injected (no fallbacks).

    Use this fixture when you want full control over all dependencies without
    relying on __post_init__ fallbacks. This is the preferred pattern for new tests.

    Usage:
        def test_something(explicit_orchestrator_deps, sample_config):
            deps = explicit_orchestrator_deps(sample_config, mock_repo_host, mock_wt_manager)
            orchestrator = Orchestrator(
                config=sample_config,
                _repository_host=mock_repo_host,
                **deps,
            )
    """
    # Use provided mocks if test requests them, otherwise create new ones
    if 'mock_terminal_plugin' in request.fixturenames:
        plugin = request.getfixturevalue('mock_terminal_plugin')
    else:
        plugin = MockTerminalPlugin()

    if 'mock_repository_host' in request.fixturenames:
        default_repo_host = request.getfixturevalue('mock_repository_host')
    else:
        default_repo_host = MockGitHubAdapter()

    mock_events = MockEventSink()
    mock_runner = MockSessionRunner(plugin)

    def create_deps(config, repo_host=None, worktree_manager=None):
        """Create all dependencies for Orchestrator constructor."""
        rh = repo_host or default_repo_host
        wm = worktree_manager or MagicMock()
        return build_test_orchestrator_deps(config, rh, mock_events, mock_runner, wm)

    return create_deps


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample agent config for testing."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Test prompt")

    return AgentConfig(
        prompt_path=prompt_file,
        model="sonnet",
        timeout_minutes=45,
    )


@pytest.fixture
def sample_config(sample_agent_config, tmp_path):
    """Create a sample Config object for testing."""
    config = Config()
    config.repo = "test/repo"  # Required for session launching
    config.repo_root = tmp_path  # Set repo_root for worktree operations
    config.worktree_base = tmp_path  # Top-level worktree_base (no per-agent)
    config.agents["agent:web"] = sample_agent_config
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.ui_mode = "tmux"
    config.setup_worktree = []
    # Use temp directory for state file to isolate tests
    config.state_file = tmp_path / ".issue-orchestrator" / "state.json"
    # Tests are not exercising hook enforcement.
    config.dangerous = DangerousConfig(allow_unsupported_agents=True)
    return config


@pytest.fixture
def sample_orchestrator(sample_config, mock_repository_host):
    """Create an Orchestrator with all dependencies explicitly injected.

    Uses the explicit DI pattern - no autouse fixture patching needed.
    """
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    runner = MockSessionRunner()
    runner.plugin.session_exists_override = False
    wt_manager = GitWorktreeManager()
    wc = GitWorkingCopy()

    deps = build_test_orchestrator_deps(
        sample_config,
        mock_repository_host,
        MockEventSink(),
        runner,
        wt_manager,
        working_copy=wc,
    )

    return Orchestrator(config=sample_config, deps=deps)


@pytest.fixture
def sample_issues():
    """Create sample issues for testing."""
    return [
        Issue(
            number=1,
            title="High priority task",
            labels=["priority:high", "agent:web"],
            body="This is a high priority issue",
        ),
        Issue(
            number=2,
            title="Medium priority task",
            labels=["priority:medium", "agent:web"],
            body="This is a medium priority issue",
        ),
        Issue(
            number=3,
            title="Low priority task",
            labels=["priority:low", "agent:mobile"],
            body="This is a low priority issue",
        ),
        Issue(
            number=4,
            title="Blocked issue",
            labels=["blocked", "agent:web"],
            body="This issue is blocked by #1",
        ),
        Issue(
            number=5,
            title="In-progress issue",
            labels=["in-progress", "agent:web"],
            body="Currently being worked on",
        ),
    ]


@pytest.fixture
def make_session(sample_agent_config, tmp_path):
    """Factory fixture to create Session objects with proper SessionKey.

    Usage:
        def test_something(make_session):
            session = make_session(issue_number=123)
            session = make_session(issue_number=456, task=TaskKind.REVIEW)
    """
    def _make_session(
        issue_number: int = 123,
        issue_title: str = "Test Issue",
        issue_labels: list[str] | None = None,
        task: TaskKind = TaskKind.CODE,
        repo: str = "test/repo",
        terminal_id: str | None = None,
        branch_name: str | None = None,
        worktree_path: Path | None = None,
        agent_config: AgentConfig | None = None,
    ) -> Session:
        issue = Issue(
            number=issue_number,
            title=issue_title,
            labels=issue_labels or [],
        )
        issue_key = FakeIssueKey(name=str(issue_number))
        session_key = SessionKey(issue=issue_key, task=task)

        # Generate defaults based on task type
        if terminal_id is None:
            if task == TaskKind.REVIEW:
                terminal_id = f"review-{issue_number}"
            elif task == TaskKind.REWORK:
                terminal_id = f"rework-{issue_number}"
            else:
                terminal_id = f"issue-{issue_number}"

        if branch_name is None:
            branch_name = f"issue-{issue_number}"

        if worktree_path is None:
            worktree_path = tmp_path / f"worktree-{issue_number}"
            worktree_path.mkdir(parents=True, exist_ok=True)

        run_assets = FileSystemSessionOutput().start_run(
            worktree_path=worktree_path,
            session_name=terminal_id,
            issue_number=issue_number,
            agent_label=issue.agent_type,
        )
        return Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config or sample_agent_config,
            terminal_id=terminal_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            run_assets=run_assets,
        )

    return _make_session


@pytest.fixture
def sample_issue_with_dependencies():
    """Create issues with various dependency mentions for testing."""
    return [
        Issue(
            number=101,
            title="First issue",
            labels=["priority:high"],
            body="This is the first issue",
        ),
        Issue(
            number=102,
            title="Depends on first",
            labels=["priority:medium"],
            body="This is blocked by #101",
        ),
        Issue(
            number=103,
            title="Multiple dependencies",
            labels=["priority:low"],
            body="Blocked by #101 and depends on #102",
        ),
        Issue(
            number=104,
            title="After implementation",
            labels=["priority:medium"],
            body="This should be done after #101",
        ),
        Issue(
            number=105,
            title="Requires other work",
            labels=["priority:high"],
            body="Requires #101 and #102 to be completed",
        ),
        Issue(
            number=106,
            title="Waiting for someone",
            labels=["priority:low"],
            body="Waiting for #104 to complete before starting",
        ),
    ]


@pytest.fixture
def mock_github_api():
    """Create a mock GitHub API object."""
    mock = MagicMock()
    mock.get_issues.return_value = []
    mock.add_label.return_value = None
    mock.remove_label.return_value = None
    return mock


@pytest.fixture
def mock_config_yaml(tmp_path):
    """Create a temporary config YAML file."""
    config_content = """
worktrees:
  base: /path/to/worktrees

agents:
  agent:web:
    prompt: /path/to/web_prompt.txt
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: /path/to/mobile_prompt.txt
    model: sonnet
    timeout_minutes: 60

execution:
  concurrency:
    max_concurrent_sessions: 3
    session_timeout_minutes: 45

labels:
  in_progress: in-progress
  blocked: blocked
  needs_human: needs-human

repo:
  name: owner/repo
"""
    config_file = tmp_path / ".issue-orchestrator.yaml"
    config_file.write_text(config_content)
    return config_file


# =============================================================================
# Provider availability (#6999)
# =============================================================================


def make_provider_availability(config, provider_resilience=None):
    """A real :class:`ProviderAvailabilityPolicy` over an in-memory circuit.

    Completion planning asks this owner for the provider-blocked transition
    instead of building a raw label mutation, so tests that construct a
    completion planner/handler must supply a real one (#6999 F5/A2). Passing an
    existing ``provider_resilience`` shares the circuit under test.
    """
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.control.provider_availability import (
        ProviderAvailabilityPolicy,
    )
    from issue_orchestrator.control.provider_resilience import (
        ProviderResilienceManager,
    )
    from issue_orchestrator.ports import InMemoryProviderCircuitStore
    from issue_orchestrator.ports.event_sink import NullEventSink

    if provider_resilience is None:
        provider_resilience = ProviderResilienceManager(
            config=config.provider_resilience,
            store=InMemoryProviderCircuitStore(),
            events=NullEventSink(),
        )
    return ProviderAvailabilityPolicy(
        config, provider_resilience, LabelManager(config)
    )
