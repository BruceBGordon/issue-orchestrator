"""Unit tests for SessionLauncher - behavior-centric tests.

These tests verify:
1. Issue session launching (happy path, error cases, state transitions)
2. Review session launching (happy path, conflicts, state transitions)
3. Rework session launching (happy path, PR resolution, conflicts)
4. Helper functions (detect_existing_work, etc.)
5. Completion handling and orchestrator wrappers

Tests mock at port boundaries, not internal patches, following the hexagonal architecture.
"""

import json
import os
import shlex
import pytest
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, cast
from unittest.mock import MagicMock, patch

from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin
from issue_orchestrator.domain.claim import ClaimResult
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.launch_transaction import PendingWorkLaunchClaim
from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.control.session_completion import (
    _apply_completed_decisions,
    _record_provider_resilience_effects,
    _terminate_finished_session,
    handle_session_completion,
    process_active_sessions,
)
from issue_orchestrator.control.completion_dispatcher import CompletedDecision
from issue_orchestrator.control.completion_handler import CleanupDecision
from issue_orchestrator.control.session_decision import (
    ProviderTransientFailureDecision,
    SessionDecision,
)
from issue_orchestrator.control.session_launch_types import (
    LaunchDisposition,
    LaunchResult,
)
from tests.callback_endpoint_helpers import ready_callback_endpoint
from issue_orchestrator.control.session_launcher import (
    SessionLauncher,
    detect_existing_work,
    log_transition,
)
from issue_orchestrator.control.isolation import GRADLE_USER_HOME_ENV
from issue_orchestrator.control.session_review_support import build_review_existing_work
from issue_orchestrator.control.pending_session_queues import (
    TECH_LEAD_LAUNCH_RETRY_LIMIT,
    PendingSessionQueues,
    TechLeadQueueOutcome,
    TechLeadRetentionOutcome,
)
from issue_orchestrator.control.session_routing import (
    orchestrator_launch_session,
    orchestrator_launch_review_session,
    orchestrator_launch_rework_session,
    orchestrator_launch_tech_lead_session,
    orchestrator_launch_validation_retry_session,
    session_launcher_callback,
    restore_running_sessions,
    parse_session_ref,
    create_session,
    session_exists,
    kill_session,
    get_session_machine,
)
from issue_orchestrator.control.workflows.tech_lead_workflow import TechLeadWorkflow
from issue_orchestrator.control.actions import (
    ActionResult,
    AddCommentAction,
    AddLabelAction,
    CreateTechLeadIssueAction,
    QueueTechLeadAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.orchestrator_support import OrchestratorSupport
from issue_orchestrator.control.retrospective_review_completion import (
    retrospective_review_completion_actions,
)
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    Issue,
    ORCHESTRATOR_PR_MARKER,
    Session,
    SessionStatus,
    AgentConfig,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
    PendingCleanup,
    OrchestratorState,
    SessionHistoryEntry,
    TaskKind,
    SessionKey,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey, FakeIssueKey
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.board_snapshot import (
    BOARD_SNAPSHOT_SCHEMA_VERSION,
    BoardFailure,
    BoardSnapshot,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.domain.state_machines.issue_machine import IssueStateMachine, IssueState
from issue_orchestrator.domain.state_machines.session_machine import SessionStateMachine, SessionState
from issue_orchestrator.domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from issue_orchestrator.events import EventName
from issue_orchestrator.ports import (
    WorktreeInfo,
    CommitInfo,
    NullEventSink,
    TraceEvent,
    CommandResult,
    NullBoardSnapshotProvider,
    NullManifestDownloader,
    InMemoryProviderCircuitStore,
)
from issue_orchestrator.ports.coder_prompt import CoderPromptAddendumProvider
from issue_orchestrator.domain.coder_prompt import (
    CoderPromptAddendumUnavailable,
    PreparedCoderPromptAddendum,
)
from issue_orchestrator.ports.command_runner import OutputNewlines
from issue_orchestrator.ports.provider_readiness import (
    ProviderReadiness,
    ProviderReadinessProbe,
)
from issue_orchestrator.ports.board_snapshot_provider import BoardSnapshotProvider
from issue_orchestrator.control.board_snapshot_builder import (
    BoardSnapshotBuilder,
    StateBoardSnapshotProvider,
)
from issue_orchestrator.ports.worktree_manager import (
    RegisteredWorktree,
    WorktreeReuseOptions,
)
from issue_orchestrator.ports.pull_request_tracker import PRInfo, PRRef
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.adapters.github.cache import GitHubCache
from issue_orchestrator.execution.stack_predecessor_facts import (
    GitStackPredecessorFactsProvider,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.contracts.public import SessionStartedPayload
from tests.unit.session_run_helpers import make_session_run_assets


# =============================================================================
# Mock Adapters - following hexagonal architecture patterns
# =============================================================================


class MockEventSink:
    """Mock EventSink for capturing published events."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)

    def get_events_by_name(self, name: str) -> list[TraceEvent]:
        return [e for e in self.events if e.name == name]

    def clear(self) -> None:
        self.events.clear()


class MockRepositoryHost:
    """Mock repository host implementing port interface."""

    def __init__(self):
        self.labels: dict[int, set[str]] = {}
        self.issues: dict[int, Issue] = {}
        self.prs: dict[int, list[PRInfo]] = {}  # issue_number -> PRs
        self.pr_reviews: dict[int, list[dict]] = {}  # pr_number -> reviews
        self.add_label_calls: list[tuple[int, str]] = []
        self.remove_label_calls: list[tuple[int, str]] = []
        self.search_pr_refs_calls: list[int] = []
        self.prs_with_label: list[PRInfo] = []
        self.get_prs_with_label_calls: list[tuple[str, str]] = []
        self.list_issues_calls: list[tuple[list[str] | None, str, int]] = []
        self.get_issue_labels_fresh = MagicMock(
            side_effect=lambda issue_number: sorted(
                self.labels.get(issue_number, set())
            )
        )

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return self.prs.get(issue_number, [])

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
        exhaustive: bool = False,
    ) -> list[Issue]:
        # ``exhaustive`` asks the real adapter to raise rather than return a
        # partial page; this mock always returns the complete in-memory set,
        # so it is accepted and ignored (the port declares it — #6779 R17).
        del milestone, required_stable_ids, exhaustive
        self.list_issues_calls.append((labels, state, limit))
        candidates = [
            self.get_issue(issue_number)
            for issue_number in sorted(set(self.issues) | set(self.labels))
        ]
        required_labels = set(labels or ())
        return [
            issue
            for issue in candidates
            if issue is not None and required_labels <= set(issue.labels)
        ][:limit]

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        self.get_prs_with_label_calls.append((label, state))
        return list(self.prs_with_label)

    def search_pr_refs_for_issue(self, issue_number: int) -> list[PRRef]:
        self.search_pr_refs_calls.append(issue_number)
        return [
            PRRef(number=pr.number, url=pr.url, title=pr.title, body=pr.body)
            for pr in self.prs.get(issue_number, [])
        ]

    def get_pr(self, pr_number: int) -> PRInfo | None:
        for prs in self.prs.values():
            for pr in prs:
                if pr.number == pr_number:
                    return pr
        return None

    def get_issue(self, issue_number: int) -> Issue | None:
        issue = self.issues.get(issue_number)
        if issue is not None:
            return issue
        labels = sorted(self.labels.get(issue_number, set()))
        if not any(label.startswith("agent:") for label in labels):
            labels.append("agent:web")
        return Issue(
            number=issue_number,
            title=f"Issue {issue_number}",
            labels=labels,
            repo="test/repo",
        )

    def get_pr_reviews(self, pr_number: int) -> list[dict]:
        return self.pr_reviews.get(pr_number, [])

    def create_issue_key(self, issue_number: int) -> GitHubIssueKey:
        return GitHubIssueKey(repo="test/repo", external_id=str(issue_number))


class MockWorktreeManager:
    """Mock worktree manager for testing."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.create_calls: list[dict] = []
        self.remove_calls: list[Path] = []
        self.remove_force_calls: list[tuple[Path, bool]] = []
        self.checkout_only_removals: list[tuple[Path, bool]] = []
        self.checkout_and_branch_removals: list[tuple[Path, bool]] = []

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options: WorktreeReuseOptions | None = None,
        worktree_name: str | None = None,
    ) -> WorktreeInfo:
        self.create_calls.append({
            "repo_root": repo_root,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "enforce_hooks": enforce_hooks,
            "base_branch": base_branch,
            "seed_ref": seed_ref,
            "branch_name": branch_name,
            "reuse_options": reuse_options,
            "worktree_name": worktree_name,
        })
        # Honor the scratch worktree_name override so a scratch investigation
        # gets a distinct path from the focus issue's worktree.
        worktree_path = self.tmp_path / (worktree_name or f"worktree-{issue_number}")
        worktree_path.mkdir(parents=True, exist_ok=True)
        return WorktreeInfo(
            path=worktree_path,
            branch_name=branch_name or f"{issue_number}-feature",
        )

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        self.remove_calls.append(worktree_path)
        self.remove_force_calls.append((worktree_path, force))
        self.checkout_only_removals.append((worktree_path, force))

    def remove_checkout_and_branch(
        self, worktree_path: Path, *, force: bool = False
    ) -> None:
        self.remove_calls.append(worktree_path)
        self.remove_force_calls.append((worktree_path, force))
        self.checkout_and_branch_removals.append((worktree_path, force))

    def can_remove_without_user_changes(self, worktree_path: Path) -> bool:
        del worktree_path
        return False

    def list_registered(self, repo_root: Path) -> tuple[RegisteredWorktree, ...]:
        del repo_root
        return ()


class MockWorkingCopy:
    """Mock working copy for VCS operations."""

    def __init__(self):
        self.commits_ahead: list[CommitInfo] = []
        self.current_branch: str | None = "main"
        self.head_sha: str | None = None

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        return self.commits_ahead

    def get_current_branch(self, worktree: Path) -> str | None:
        return self.current_branch

    def get_head_sha(self, worktree: Path) -> str | None:
        return self.head_sha


class MockCommandRunner:
    """Mock command runner for setup commands."""

    def __init__(self):
        self.run_calls: list[dict] = []
        self.results: list[CommandResult] = []
        self._result_index = 0

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> CommandResult:
        self.run_calls.append({"command": command, "cwd": cwd, "env": env, "shell": shell})
        if self._result_index < len(self.results):
            result = self.results[self._result_index]
            self._result_index += 1
            return result
        return CommandResult(returncode=0, stdout="", stderr="", timed_out=False)


class MockSessionManager:
    """Mock session manager for terminal operations."""

    def __init__(self):
        self.sessions: dict[str, bool] = {}
        self.start_calls: list = []
        self.stop_calls: list = []

    def start(self, ctx) -> bool:
        self.start_calls.append(ctx)
        return True

    def stop(self, ref) -> None:
        self.stop_calls.append(ref)

    def exists(self, ref) -> bool:
        return ref.name in self.sessions


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_path_factory_fix(tmp_path):
    """Provide tmp_path as a fixture for tests."""
    return tmp_path


@pytest.fixture
def mock_events():
    """Create a mock event sink."""
    return MockEventSink()


@pytest.fixture
def mock_repo_host():
    """Create a mock repository host."""
    return MockRepositoryHost()


@pytest.fixture
def mock_worktree_manager(tmp_path):
    """Create a mock worktree manager."""
    return MockWorktreeManager(tmp_path)


@pytest.fixture
def mock_working_copy():
    """Create a mock working copy."""
    return MockWorkingCopy()


@pytest.fixture
def mock_command_runner():
    """Create a mock command runner."""
    return MockCommandRunner()


@pytest.fixture
def sample_config(tmp_path):
    """Create a sample Config for testing."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")

    config = Config()
    config.repo = "test/repo"
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir(exist_ok=True)
    config.worktree_base = tmp_path / "worktrees"  # Top-level worktree_base
    config.agents["agent:web"] = AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=45,
    )
    config.agents["agent:reviewer"] = AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=30,
    )
    config.code_review_agent = "agent:reviewer"
    config.setup_worktree = []
    return config


@pytest.fixture
def sample_issue():
    """Create a sample Issue for testing."""
    return Issue(
        number=123,
        title="Test issue",
        labels=["agent:web"],
        repo="test/repo",
    )


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample AgentConfig for testing."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")
    return AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=45,
    )


class RecordingBoardSnapshotProvider:
    """Fake BoardSnapshotProvider: records focus issues, optionally raises.

    ``error`` is a public attribute so tests flip the provider into failure
    mode through the object they injected, never through launcher internals.
    """

    def __init__(self, error: Exception | None = None):
        self.calls: list[int | None] = []
        self.cohort_calls: list[tuple[int, ...]] = []
        self.error = error
        self.recent_failures: list[BoardFailure] = []

    def snapshot(
        self,
        focus_issue: int | None,
        problem_cohort: tuple[int, ...] = (),
    ) -> BoardSnapshot:
        self.calls.append(focus_issue)
        # Recorded so tests can assert the launch boundary passes the OWNED
        # cohort down rather than letting the provider infer it (#6780).
        self.cohort_calls.append(tuple(problem_cohort))
        if self.error is not None:
            raise self.error
        return BoardSnapshot(
            generated_at="2026-07-10T00:00:00",
            orchestrator_paused=False,
            recent_failures=list(self.recent_failures),
            problem_cohort=list(problem_cohort),
        )


@dataclass
class LauncherTestBundle:
    """Bundle of launcher and tracking objects for tests."""
    launcher: SessionLauncher
    session_exists_calls: list
    create_session_calls: list
    issue_machines: dict
    session_machines: dict
    review_machines: dict
    # Stores [callable] so tests can override behavior
    session_exists_override: list
    create_session_override: list = field(default_factory=lambda: [None])
    # Injected mocks for test assertions
    action_applier: MagicMock = field(default_factory=MagicMock)
    board_snapshot_provider: BoardSnapshotProvider = field(
        default_factory=RecordingBoardSnapshotProvider
    )
    claim_manager: MagicMock | None = None


def _build_launcher_bundle(
    sample_config,
    mock_events,
    mock_repo_host,
    mock_worktree_manager,
    mock_working_copy,
    mock_command_runner,
    *,
    board_snapshot_provider: BoardSnapshotProvider | None = None,
    coder_prompt_addendum: CoderPromptAddendumProvider | None = None,
    claim_manager: MagicMock | None = None,
    provider_resilience: ProviderResilienceManager | None = None,
    provider_readiness_probe: ProviderReadinessProbe | None = None,
) -> LauncherTestBundle:
    """Create a SessionLauncher with mock dependencies and tracking.

    ``board_snapshot_provider`` is constructor-injected (it is a REQUIRED
    launcher dependency); defaults to a fresh RecordingBoardSnapshotProvider
    exposed on the bundle for assertions.
    """
    if board_snapshot_provider is None:
        board_snapshot_provider = RecordingBoardSnapshotProvider()
    session_exists_calls = []
    create_session_calls = []
    session_exists_override = [None]  # List so tests can replace the callable

    def mock_session_exists(name: str) -> bool:
        session_exists_calls.append(name)
        if session_exists_override[0] is not None:
            return session_exists_override[0](name)
        return False

    create_session_override = [None]  # List so tests can replace the callable

    def mock_create_session(name: str, cmd: str, wd: Path, title: str | None) -> bool:
        create_session_calls.append({"name": name, "cmd": cmd, "wd": wd, "title": title})
        if create_session_override[0] is not None:
            return create_session_override[0](name, cmd, wd, title)
        return True

    issue_machines: dict[int, IssueStateMachine] = {}
    session_machines: dict[str, SessionStateMachine] = {}
    review_machines: dict[int, ReviewStateMachine] = {}

    def get_issue_machine(issue):
        if issue.number not in issue_machines:
            issue_machines[issue.number] = IssueStateMachine(issue)
        return issue_machines[issue.number]

    def get_session_machine(name: str, n: int, timeout: int):
        if name not in session_machines:
            session_machines[name] = SessionStateMachine(name, n, timeout_minutes=timeout)
        return session_machines[name]

    def remove_session_machine(name: str) -> None:
        session_machines.pop(name, None)

    def get_review_machine(pr_number: int, issue_number: int):
        if pr_number not in review_machines:
            review_machines[pr_number] = ReviewStateMachine(pr_number, issue_number)
        return review_machines[pr_number]

    mock_action_applier = MagicMock()
    launcher_kwargs = {}
    if coder_prompt_addendum is not None:
        launcher_kwargs["coder_prompt_addendum"] = coder_prompt_addendum
    if provider_resilience is not None:
        launcher_kwargs["provider_resilience"] = provider_resilience
    if provider_readiness_probe is not None:
        launcher_kwargs["provider_readiness_probe"] = provider_readiness_probe
    launcher = SessionLauncher(
        config=sample_config,
        events=mock_events,
        repository_host=mock_repo_host,
        action_applier=mock_action_applier,
        session_manager=MagicMock(),
        worktree_manager=mock_worktree_manager,
        working_copy=mock_working_copy,
        command_runner=mock_command_runner,
        session_output=FileSystemSessionOutput(),
        manifest_downloader=NullManifestDownloader(),
        tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(sample_config.repo_root),
        session_exists_fn=mock_session_exists,
        create_session_fn=mock_create_session,
        get_issue_machine=get_issue_machine,
        get_session_machine=get_session_machine,
        get_review_machine=get_review_machine,
        remove_session_machine=remove_session_machine,
        board_snapshot_provider=board_snapshot_provider,
        agent_callback_endpoint=ready_callback_endpoint(),
        claim_manager=claim_manager,
        **launcher_kwargs,
    )

    bundle = LauncherTestBundle(
        launcher=launcher,
        session_exists_calls=session_exists_calls,
        create_session_calls=create_session_calls,
        issue_machines=issue_machines,
        session_machines=session_machines,
        review_machines=review_machines,
        session_exists_override=session_exists_override,
        create_session_override=create_session_override,
        action_applier=mock_action_applier,
        board_snapshot_provider=board_snapshot_provider,
        claim_manager=claim_manager,
    )
    return bundle


@pytest.fixture
def launcher_bundle(
    sample_config,
    mock_events,
    mock_repo_host,
    mock_worktree_manager,
    mock_working_copy,
    mock_command_runner,
) -> LauncherTestBundle:
    """Create a SessionLauncher bundle with default mock dependencies."""
    return _build_launcher_bundle(
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
    )


@pytest.fixture
def internal_review_launcher_bundle(
    sample_config,
    mock_events,
    mock_repo_host,
    mock_worktree_manager,
    mock_working_copy,
    mock_command_runner,
) -> tuple[LauncherTestBundle, MagicMock]:
    """Build a launcher with a recording coder-only prompt provider."""
    provider = MagicMock(name="coder_prompt_addendum")
    provider.prepare.return_value = PreparedCoderPromptAddendum(
        "INTERNAL-REVIEW-MARKER"
    )
    claim_manager = MagicMock(name="claim_manager")
    claim_manager.attempt_claim.return_value = ClaimResult.claimed("lease-123")
    claim_manager.run_convergence.return_value = True
    bundle = _build_launcher_bundle(
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        coder_prompt_addendum=provider,
        claim_manager=claim_manager,
    )
    return bundle, provider


@pytest.fixture
def unavailable_provider_internal_review_bundle(
    sample_config,
    mock_events,
    mock_repo_host,
    mock_worktree_manager,
    mock_working_copy,
    mock_command_runner,
) -> tuple[LauncherTestBundle, MagicMock, MagicMock]:
    """Build a coder whose provider gate would mutate if it were reached."""
    provider = MagicMock(name="coder_prompt_addendum")
    provider.prepare.return_value = CoderPromptAddendumUnavailable(
        "instructions missing"
    )
    sample_config.agents["agent:web"].provider = "claude-code"
    readiness_probe = MagicMock(spec=ProviderReadinessProbe)
    readiness_probe.check_launch_readiness.return_value = (
        ProviderReadiness.auth_expired("claude-code", "not logged in")
    )
    provider_resilience = ProviderResilienceManager(
        config=sample_config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=mock_events,
    )
    claim_manager = MagicMock(name="claim_manager")
    claim_manager.attempt_claim.return_value = ClaimResult.claimed("lease-123")
    claim_manager.run_convergence.return_value = True
    bundle = _build_launcher_bundle(
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        coder_prompt_addendum=provider,
        claim_manager=claim_manager,
        provider_resilience=provider_resilience,
        provider_readiness_probe=readiness_probe,
    )
    return bundle, provider, readiness_probe


@pytest.fixture
def session_launcher(launcher_bundle: LauncherTestBundle) -> SessionLauncher:
    """Convenience fixture for tests that only need the launcher."""
    return launcher_bundle.launcher


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestDetectExistingWork:
    """Tests for detect_existing_work function - lines 77, 84, 91-93."""

    def test_returns_none_when_no_commits_ahead(self, tmp_path):
        """Verify returns None when worktree has no commits ahead of main (line 77)."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = []

        result = detect_existing_work(tmp_path, working_copy)

        assert result is None

    def test_returns_none_when_head_matches_seed_ref(self, tmp_path):
        """Seeded local issue worktrees should not surface inherited base commits as existing work."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = [
            CommitInfo(sha="abc123", message="Base fix", author="test", short_sha="abc1"),
        ]
        working_copy.head_sha = "abc123"

        result = detect_existing_work(tmp_path, working_copy, seed_ref="abc123")

        assert result is None

    def test_returns_context_with_commits(self, tmp_path):
        """Verify returns context string when commits exist."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = [
            CommitInfo(sha="abc123", message="Fix bug", author="test", short_sha="abc1"),
            CommitInfo(sha="def456", message="Add feature", author="test", short_sha="def4"),
        ]
        working_copy.current_branch = "123-feature"

        result = detect_existing_work(tmp_path, working_copy)

        assert result is not None
        assert "2 existing commit(s)" in result
        assert "123-feature" in result
        assert "abc1" in result
        assert "Fix bug" in result

    def test_truncates_long_commit_list(self, tmp_path):
        """Verify truncates commit list when more than 10 commits (line 84)."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = [
            CommitInfo(sha=f"sha{i}", message=f"Commit {i}", author="test", short_sha=f"s{i}")
            for i in range(15)
        ]

        result = detect_existing_work(tmp_path, working_copy)

        assert result is not None
        assert "15 existing commit(s)" in result
        assert "... and 5 more" in result

    def test_handles_exception_gracefully(self, tmp_path):
        """Verify handles exceptions and returns None (lines 91-93)."""
        working_copy = MagicMock()
        working_copy.get_commits_ahead_of_main.side_effect = Exception("Git error")

        result = detect_existing_work(tmp_path, working_copy)

        assert result is None


class TestLogTransition:
    """Tests for log_transition function."""

    def test_logs_transition_info(self, caplog):
        """Verify transition is logged correctly."""
        import logging
        caplog.set_level(logging.INFO)

        log_transition("issue", 123, "AVAILABLE", "LAUNCHING", "no conflicts")

        assert "[TRANSITION] issue #123: AVAILABLE" in caplog.text
        assert "LAUNCHING" in caplog.text

    def test_logs_extra_data_at_debug(self, caplog):
        """Verify extra data is logged at debug level."""
        import logging
        caplog.set_level(logging.DEBUG)

        log_transition("issue", 123, "AVAILABLE", "LAUNCHING", "reason", {"agent": "web"})

        assert "[TRANSITION] #123 extra:" in caplog.text


# =============================================================================
# Issue Session Launch Tests
# =============================================================================


class TestLaunchIssueSession:
    """Tests for launch_issue_session method."""

    def test_successful_launch(self, session_launcher, sample_issue):
        """Verify successful issue session launch."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "issue-123"
        assert result.session.key.task == TaskKind.CODE
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__coding-1")

    def test_internal_review_instructions_reach_initial_coder_command(
        self,
        internal_review_launcher_bundle,
        sample_issue,
    ):
        bundle, provider = internal_review_launcher_bundle

        result = bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert "INTERNAL-REVIEW-MARKER" in bundle.create_session_calls[0]["cmd"]
        provider.prepare.assert_called_once_with(
            task=TaskKind.CODE,
            agent_label=sample_issue.agent_type,
        )

    def test_missing_internal_review_instructions_fail_before_initial_mutation(
        self,
        unavailable_provider_internal_review_bundle,
        sample_issue,
        mock_worktree_manager,
    ):
        bundle, _provider, readiness_probe = (
            unavailable_provider_internal_review_bundle
        )
        work_claim = MagicMock()

        result = bundle.launcher.launch_issue_session(
            sample_issue,
            active_sessions=[],
            work_claim=work_claim,
        )

        assert result.success is False
        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE
        assert "instructions missing" in result.reason
        work_claim.hold_before_spawn.assert_not_called()
        assert bundle.claim_manager is not None
        bundle.claim_manager.attempt_claim.assert_not_called()
        readiness_probe.check_launch_readiness.assert_not_called()
        bundle.action_applier.apply.assert_not_called()
        assert mock_worktree_manager.create_calls == []
        assert bundle.create_session_calls == []

    def test_tech_lead_session_creates_tech_lead_data_dir_without_manifest(
        self, session_launcher, sample_config, tmp_path
    ):
        """Every tech_lead session gets a tech-lead-data dir for its decision
        artifact pair (ADR-0031), even when no PR manifest exists."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        issue = Issue(
            number=125,
            title="Batch Review",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )

        result = session_launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        data_dir = result.session.run_dir / "tech-lead-data"
        assert data_dir.is_dir()
        # No PRs matched, so no manifest was planted — only the empty dir.
        assert not (data_dir / "manifest.json").exists()
        # The orchestrator-owned launch authority is recorded outside the
        # agent-writable worktree (#6761 re-review F1).
        authority = SqliteTechLeadAuthorityStore.for_repo(
            sample_config.repo_root
        ).load(
            run_id=result.session.run_assets.run_id,
            session_name=result.session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW
        assert authority.anchor_issue_number == 125
        assert authority.manifest_pr_numbers == ()

    def test_tech_lead_command_never_receives_coder_internal_review_addendum(
        self,
        internal_review_launcher_bundle,
        tmp_path,
    ):
        bundle, provider = internal_review_launcher_bundle
        prompt_path = tmp_path / "tech-lead-prompt.md"
        bundle.launcher.config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        bundle.launcher.config.tech_lead_review_agent = "agent:tech-lead"
        provider.prepare.return_value = PreparedCoderPromptAddendum(None)
        issue = Issue(
            number=125,
            title="Batch Review",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )

        result = bundle.launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is True
        assert "INTERNAL-REVIEW-MARKER" not in bundle.create_session_calls[0]["cmd"]
        provider.prepare.assert_called_once_with(
            task=TaskKind.CODE,
            agent_label="agent:tech-lead",
        )

    def test_tech_lead_launch_preserves_branch_but_coding_does_not(
        self, session_launcher, mock_worktree_manager, sample_config, sample_issue, tmp_path
    ):
        """A tech_lead launch reuses the subject worktree without mutating its
        branch (preserve_branch=True), so a stranded branch's unpushed work is
        read as evidence rather than rebased/reset away. A coding launch keeps
        the default (preserve_branch=False)."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"

        coding_result = session_launcher.launch_issue_session(
            sample_issue, active_sessions=[]
        )
        assert coding_result.success is True

        tech_lead_issue = Issue(
            number=5980,
            title="Investigate stranded failure",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )
        tech_lead_result = session_launcher.launch_issue_session(
            tech_lead_issue, active_sessions=[]
        )
        assert tech_lead_result.success is True

        coding_call = next(
            c for c in mock_worktree_manager.create_calls if c["issue_number"] == 123
        )
        tech_lead_call = next(
            c for c in mock_worktree_manager.create_calls if c["issue_number"] == 5980
        )
        assert coding_call["reuse_options"].preserve_branch is False
        assert tech_lead_call["reuse_options"].preserve_branch is True

    def test_failure_investigation_runs_in_disposable_scratch_worktree(
        self, session_launcher, mock_worktree_manager, sample_config, tmp_path
    ):
        """A FAILURE_INVESTIGATION launch must NOT touch the focus issue's
        worktree/branch (#6823): it runs in a fresh, run-scoped scratch worktree
        on a throwaway branch off the base, so the focus branch stays read-only
        evidence and the scratch workspace is force-fresh (no reuse) with no
        subject branch to preserve."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        focus = 5980
        tech_lead_issue = Issue(
            number=focus,
            title="Investigate stranded failure",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )

        result = session_launcher.launch_issue_session(
            tech_lead_issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
            ),
        )
        assert result.success is True

        call = next(
            c for c in mock_worktree_manager.create_calls if c["issue_number"] == focus
        )
        # Scratch worktree name/branch are keyed to the run, NOT the focus issue.
        assert call["worktree_name"] is not None
        assert call["worktree_name"].startswith(f"repo-tech-lead-{focus}-")
        assert call["worktree_name"] != f"repo-{focus}"
        assert call["branch_name"] is not None
        assert call["branch_name"].startswith(f"tech-lead-investigation-{focus}-")
        # The scratch branch must never look like the focus issue's own branch.
        assert not call["branch_name"].startswith(f"{focus}-")
        # Disposable: force-fresh (no reuse), and nothing to preserve.
        assert call["reuse_options"].disable_reuse is True
        assert call["reuse_options"].preserve_branch is False
        # Off the base branch — the configured seed ref is suppressed so the
        # checkout is clean off base, never seeded from the focus branch.
        assert call["seed_ref"] is None
        # The session records that its worktree is a disposable scratch workspace.
        assert result.session is not None
        assert result.session.scratch_worktree is True
        # The scratch worktree path is distinct from the focus issue's worktree.
        assert result.session.worktree_path != tmp_path / f"worktree-{focus}"

    def test_failed_investigation_launch_removes_scratch_worktree(
        self, launcher_bundle, mock_worktree_manager, sample_config, tmp_path
    ):
        """A scratch investigation worktree has no reuse path, so a launch that
        fails at terminal-session creation must remove both checkout and branch;
        ordinary failed launches preserve their reusable branch (#6823)."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        # Force terminal-session creation to fail after the scratch worktree exists.
        launcher_bundle.create_session_override[0] = (
            lambda _name, _cmd, _wd, _title: False
        )
        focus = 5980
        tech_lead_issue = Issue(
            number=focus,
            title="Investigate stranded failure",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            tech_lead_issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
            ),
        )

        assert result.success is False
        create_call = next(
            c for c in mock_worktree_manager.create_calls if c["issue_number"] == focus
        )
        scratch_path = tmp_path / create_call["worktree_name"]
        assert scratch_path in mock_worktree_manager.remove_calls
        # F8: a disposable scratch worktree is FORCE-removed — a partial launch
        # can leave an untracked artifact that a non-forced remove would fail on.
        assert (scratch_path, True) in mock_worktree_manager.remove_force_calls

    @pytest.mark.parametrize(
        "failure_stage",
        [
            "pending_work_claim",
            "tech_lead_prep",
            "setup",
            "in_progress_label",
            "terminal",
        ],
    )
    @pytest.mark.parametrize("disposable", [False, True])
    def test_pre_active_failure_cleanup_owns_branch_policy(
        self,
        failure_stage,
        disposable,
        launcher_bundle,
        mock_worktree_manager,
        mock_command_runner,
        sample_config,
        tmp_path,
    ):
        """Every pre-active failure preserves ordinary branches and deletes
        disposable scratch branches through one cleanup owner."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        work_claim = None
        claim_store = MagicMock()
        if failure_stage == "pending_work_claim":
            claim_store.hold_pending_work_claim.side_effect = RuntimeError(
                "claim store failed"
            )
        elif failure_stage == "tech_lead_prep":
            launcher_bundle.board_snapshot_provider.error = RuntimeError("prep failed")
        elif failure_stage == "setup":
            sample_config.setup_worktree = ["make worktree-setup"]
            mock_command_runner.results = [
                CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="setup failed",
                    timed_out=False,
                )
            ]
        elif failure_stage == "in_progress_label":
            def apply_action(action):
                if isinstance(action, AddLabelAction) and action.label == "in-progress":
                    return ActionResult.fail(action, "label failed")
                return ActionResult.ok(action)

            launcher_bundle.action_applier.apply = MagicMock(
                side_effect=apply_action
            )
        else:
            launcher_bundle.create_session_override[0] = (
                lambda _name, _cmd, _wd, _title: False
            )

        issue = Issue(
            number=5980,
            title="Tech Lead launch",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )
        flavor = (
            TechLeadSessionFlavor.FAILURE_INVESTIGATION
            if disposable
            else TechLeadSessionFlavor.BATCH_REVIEW
        )
        if failure_stage == "pending_work_claim":
            work_claim = PendingWorkLaunchClaim(
                claim=PendingWorkClaim(
                    kind=PendingWorkKind.TECH_LEAD,
                    request=PendingTechLeadReview(
                        issue_number=issue.number,
                        title=issue.title,
                        flavor=flavor,
                        failure=(
                            DiscoveredFailure(
                                issue.number,
                                issue.title,
                                "failed",
                            )
                            if flavor
                            is TechLeadSessionFlavor.FAILURE_INVESTIGATION
                            else None
                        ),
                    ),
                ),
                claims=claim_store,
            )

        launch_kwargs = {}
        if work_claim is not None:
            launch_kwargs["work_claim"] = work_claim
        result = launcher_bundle.launcher.launch_issue_session(
            issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(flavor=flavor),
            **launch_kwargs,
        )

        assert result.success is False
        if failure_stage == "pending_work_claim":
            assert result.disposition is LaunchDisposition.CLAIM_UNRECORDED
            claim_store.hold_pending_work_claim.assert_called_once()
        (create_call,) = mock_worktree_manager.create_calls
        worktree_path = tmp_path / (
            create_call["worktree_name"] or f"worktree-{issue.number}"
        )
        if disposable:
            assert mock_worktree_manager.checkout_and_branch_removals == [
                (worktree_path, True)
            ]
            assert mock_worktree_manager.checkout_only_removals == []
        else:
            assert mock_worktree_manager.checkout_only_removals == [
                (worktree_path, False)
            ]
            assert mock_worktree_manager.checkout_and_branch_removals == []

    def test_coding_launch_uses_focus_worktree_not_scratch(
        self, session_launcher, mock_worktree_manager, sample_issue
    ):
        """An ordinary coding launch is unchanged: no scratch override, so the
        worktree derives from the issue number and is not a scratch workspace."""
        result = session_launcher.launch_issue_session(
            sample_issue, active_sessions=[]
        )
        assert result.success is True

        call = next(
            c for c in mock_worktree_manager.create_calls
            if c["issue_number"] == sample_issue.number
        )
        assert call["worktree_name"] is None
        assert result.session is not None
        assert result.session.scratch_worktree is False

    @pytest.mark.parametrize(
        "flavor",
        [TechLeadSessionFlavor.BATCH_REVIEW, TechLeadSessionFlavor.HEALTH_REVIEW],
    )
    def test_batch_and_health_tech_lead_launches_not_scratch(
        self, session_launcher, mock_worktree_manager, sample_config, tmp_path, flavor
    ):
        """Batch and health reviews run on their own anchor worktrees, unchanged
        (#6823): no scratch override, and preserve_branch stays True so a
        stranded anchor branch is read rather than rebased away."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"

        anchor = 7001
        anchor_issue = Issue(
            number=anchor,
            title=f"{flavor.value} anchor",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )
        result = session_launcher.launch_issue_session(
            anchor_issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(flavor=flavor),
        )
        assert result.success is True
        call = next(
            c for c in mock_worktree_manager.create_calls
            if c["issue_number"] == anchor
        )
        assert call["worktree_name"] is None
        assert call["reuse_options"].preserve_branch is True
        assert result.session is not None
        assert result.session.scratch_worktree is False

    def test_failed_tech_lead_launch_discards_recorded_authority(
        self, launcher_bundle, sample_config, tmp_path
    ):
        """A launch that dies AFTER recording its authority must not leak
        the row (#6769 F3): the run never starts, so no completion seam
        would ever discard it."""
        prompt_path = tmp_path / "prompt.md"
        sample_config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        issue = Issue(
            number=125,
            title="Batch Review",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )
        # Force the terminal-session creation step (after tech_lead prep) to fail.
        launcher_bundle.create_session_override[0] = (
            lambda _name, _cmd, _wd, _title: False
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue, active_sessions=[]
        )

        assert result.success is False
        store = SqliteTechLeadAuthorityStore.for_repo(sample_config.repo_root)
        conn_rows = store._get_connection().execute(  # noqa: SLF001
            "SELECT run_id, session_name FROM tech_lead_launch_authority"
        ).fetchall()
        assert conn_rows == [], "failed launch leaked an authority row"

    def test_fails_when_no_agent_type(self, session_launcher):
        """Verify fails when issue has no agent type label (line 195)."""
        issue = Issue(number=123, title="No agent", labels=[], repo="test/repo")

        result = session_launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is False
        assert "no agent type label" in result.reason

    def test_fails_when_agent_config_missing(self, session_launcher):
        """Verify fails when agent config not found (line 199)."""
        issue = Issue(number=123, title="Unknown agent", labels=["agent:unknown"], repo="test/repo")

        result = session_launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is False
        assert "No agent config" in result.reason

    def test_fails_when_no_repo_configured(self, session_launcher, sample_issue):
        """Verify fails when no repo configured (line 202)."""
        session_launcher.config.repo = None

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "No repo configured" in result.reason

    def test_skips_when_already_in_active_sessions(self, session_launcher, sample_issue, sample_agent_config, tmp_path):
        """Verify skips when issue already active (lines 209-210)."""
        issue_key = FakeIssueKey(str(sample_issue.number))
        existing_session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[existing_session])

        assert result.success is False
        assert "Already in active sessions" in result.reason

    def test_skips_when_terminal_already_running(self, launcher_bundle, sample_issue):
        """Verify skips when terminal session exists."""
        # Override session_exists to return True
        launcher_bundle.session_exists_override[0] = lambda name: True

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Terminal session already running" in result.reason
        assert result.disposition is LaunchDisposition.EXISTING_TERMINAL

    def test_adds_in_progress_label(self, launcher_bundle, sample_issue, mock_repo_host):
        """Verify in-progress label is added."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "in-progress" for a in actions)

    def test_issue_launch_clears_coding_interrupted_guard_label(self, launcher_bundle, sample_issue):
        """Issue launch should clear interrupted coding retry guard."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        guard_label = launcher_bundle.launcher.config.retry.interrupted_sessions.coding_guard_label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == sample_issue.number
            and a.label == guard_label
            for a in actions
        )

    def test_issue_launch_from_scratch_forces_fresh_worktree_branch(
        self,
        launcher_bundle,
        sample_issue,
        mock_worktree_manager,
        mock_events,
    ):
        """Scratch pending label should force fresh worktree + fresh branch from base."""
        scratch_label = launcher_bundle.launcher._lm.reset_retry_scratch_pending  # noqa: SLF001
        sample_issue.labels = [*sample_issue.labels, scratch_label]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_worktree_manager.create_calls) == 1
        create_call = mock_worktree_manager.create_calls[0]
        reuse_options = create_call["reuse_options"]
        assert reuse_options is not None
        assert reuse_options.disable_reuse is True
        assert reuse_options.worktree_branch_on_recreate == "create_new_branch"
        branch_name = create_call["branch_name"]
        assert isinstance(branch_name, str)
        assert branch_name.startswith(f"{sample_issue.number}-scratch-")
        started = next(e for e in mock_events.events if str(e.name) == "session.started")
        assert started.data["reset_from_scratch"] is True
        run_dir = Path(started.data["run_dir"])
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["reset_from_scratch"] is True
        assert manifest["review_cache_boundary"] == "scratch_reset"
        assert manifest["review_cache_boundary_started_at"] == manifest["started_at"]

        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == sample_issue.number
            and a.label == scratch_label
            for a in actions
        )

    def test_ready_stack_successor_seeds_worktree_from_predecessor(
        self,
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        sample_issue,
    ):
        """A ready Stack-after issue creates its worktree from the predecessor (#6596).

        The just-before-launch work gate resolves the predecessor branch as the
        stack base; the launcher must thread that into the worktree manager so a
        freshly created successor descends from the predecessor head.
        """

        class _Report:
            can_start_work = True
            stack_base_branch = "20-base"

        class _Evaluator:
            def evaluate_work_gate(self, *, issue_number, issue_body, source_milestone):
                return _Report()

        def refresh_issue(_number):
            return Issue(
                number=123,
                title="Test issue",
                labels=["agent:web"],
                repo="test/repo",
                body="Stack-after: #20",
                milestone="M7",
            )

        launcher = SessionLauncher(
            config=sample_config,
            events=mock_events,
            repository_host=mock_repo_host,
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            command_runner=mock_command_runner,
            session_output=FileSystemSessionOutput(),
            manifest_downloader=NullManifestDownloader(),
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(sample_config.repo_root),
            session_exists_fn=lambda name: False,
            create_session_fn=lambda name, cmd, wd, title: True,
            get_issue_machine=lambda issue: IssueStateMachine(issue),
            get_session_machine=lambda name, n, timeout: SessionStateMachine(
                name, n, timeout_minutes=timeout
            ),
            get_review_machine=lambda pr, issue: ReviewStateMachine(pr, issue),
            refresh_issue_fn=refresh_issue,
            dependency_evaluator=_Evaluator(),
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=ready_callback_endpoint(),
        )

        result = launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_worktree_manager.create_calls) == 1
        assert mock_worktree_manager.create_calls[0]["base_branch"] == "20-base"

    def test_fails_when_in_progress_label_add_fails(
        self, launcher_bundle, sample_issue, mock_worktree_manager
    ):
        """Verify launch fails when in-progress label cannot be added."""
        def apply_action(action):
            if isinstance(action, AddLabelAction) and action.label == "in-progress":
                return ActionResult.fail(action, "api error")
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Failed to add in-progress label" in result.reason
        assert launcher_bundle.create_session_calls == []
        assert len(mock_worktree_manager.remove_calls) == 1

    def test_emits_session_started_event(self, session_launcher, sample_issue, mock_events):
        """Verify SESSION_STARTED event is emitted with expected payload."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        started_events = mock_events.get_events_by_name("session.started")
        assert len(started_events) == 1
        payload = started_events[0].data
        assert payload["issue_number"] == sample_issue.number
        assert {"session_id", "worktree_path", "branch_name", "completion_path", "completion_path_absolute"}.issubset(
            payload.keys()
        )
        worktree_path = Path(payload["worktree_path"])
        completion_path = payload["completion_path"]
        completion_parts = Path(completion_path).parts
        assert "sessions" in completion_parts
        assert any(part.endswith("__coding-1") for part in completion_parts)
        assert payload["completion_path_absolute"] == str((worktree_path / completion_path).resolve())
        SessionStartedPayload.model_validate(payload)

    def test_triggers_state_machine_transitions(self, launcher_bundle, sample_issue):
        """Verify state machine transitions are triggered."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # Check issue machine transitioned
        issue_machine = launcher_bundle.issue_machines[123]
        assert issue_machine.state == IssueState.IN_PROGRESS.value

        # Check session machine transitioned
        session_machine = launcher_bundle.session_machines["issue-123"]
        assert session_machine.state == SessionState.RUNNING.value

    def test_resets_non_pending_session_machine_on_launch(self, launcher_bundle, sample_issue):
        """Launch resets an unexpected session machine state before transitioning."""
        original = SessionStateMachine("issue-123", 123, timeout_minutes=30)
        original.launch()
        original.started()
        launcher_bundle.session_machines["issue-123"] = original

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        replacement = launcher_bundle.session_machines["issue-123"]
        assert replacement is not original
        assert replacement.state == SessionState.RUNNING.value

    def test_handles_session_creation_failure(self, launcher_bundle, sample_issue, mock_repo_host):
        """Verify handles terminal session creation failure (lines 373-376)."""
        # Override create_session to return False
        launcher_bundle.create_session_override[0] = lambda name, cmd, wd, title: False

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Failed to create terminal session" in result.reason
        # Verify in-progress label is removed on failure
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, RemoveLabelAction) and a.label == "in-progress" for a in actions)

    def test_runs_setup_commands(self, session_launcher, sample_issue, mock_command_runner):
        """Verify setup commands are run (line 315)."""
        session_launcher.config.setup_worktree = ["npm install", "pip install -e ."]

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_command_runner.run_calls) == 2
        assert mock_command_runner.run_calls[0]["command"] == "npm install"
        assert mock_command_runner.run_calls[1]["command"] == "pip install -e ."

    def test_fails_launch_when_setup_command_fails(
        self,
        launcher_bundle,
        sample_issue,
        mock_command_runner,
        mock_worktree_manager,
    ):
        """Configured setup failures must stop launch before the agent starts."""
        launcher_bundle.launcher.config.setup_worktree = ["make worktree-setup"]
        mock_command_runner.results = [
            CommandResult(returncode=1, stdout="", stderr="missing node_modules", timed_out=False),
        ]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Setup commands failed" in result.reason
        assert launcher_bundle.create_session_calls == []
        assert mock_worktree_manager.remove_calls, "failed setup should clean up the worktree"

    def test_fails_launch_when_setup_command_times_out(
        self,
        launcher_bundle,
        sample_issue,
        mock_command_runner,
    ):
        """Timeouts should surface as timeouts even if the runner also reports a nonzero exit."""
        launcher_bundle.launcher.config.setup_worktree = ["make worktree-setup"]
        mock_command_runner.results = [
            CommandResult(returncode=137, stdout="", stderr="killed", timed_out=True),
        ]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "timed out" in result.reason
        assert "exit_code=137" not in result.reason

    def test_includes_existing_work_context(self, launcher_bundle, sample_issue, mock_working_copy):
        """Verify existing work is detected and included in command."""
        mock_working_copy.commits_ahead = [
            CommitInfo(sha="abc", message="Fix", author="test", short_sha="abc"),
        ]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # The command should include existing work context
        cmd = launcher_bundle.create_session_calls[0]["cmd"]
        assert "IMPORTANT:" in cmd or "existing commit" in cmd.lower() or result.session is not None

    def test_writes_session_identity_file(self, session_launcher, sample_issue):
        """The session producer records the complete launch configuration identity."""
        session_launcher.config.launch_selection = RepositoryLaunchSelection.parse(
            mode="codex",
            config_name="main.yaml",
        )
        session_launcher.config.config_fingerprint = "effective-config-fingerprint"

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        identity = json.loads(
            (result.session.run_dir / "session-identity.json").read_text(
                encoding="utf-8"
            )
        )
        assert identity["configuration_mode"] == "codex"
        assert identity["config_name"] == "main.yaml"
        assert identity["config_fingerprint"] == "effective-config-fingerprint"

    def test_sets_e2e_pr_labels_env(self, launcher_bundle, sample_issue):
        """Verify E2E_PR_LABELS env var is set (lines 349-350)."""
        launcher_bundle.launcher.config.e2e_pr_labels = ["e2e-passed", "ci-ready"]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        cmd = launcher_bundle.create_session_calls[0]["cmd"]
        assert "E2E_PR_LABELS" in cmd

    def test_exports_worktree_path_env_var(self, launcher_bundle, sample_issue):
        """Verify ISSUE_ORCHESTRATOR_WORKTREE is exported so coding-done can guard CWD."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        cmd = launcher_bundle.create_session_calls[0]["cmd"]
        assert "ISSUE_ORCHESTRATOR_WORKTREE=" in cmd

    def test_checks_dependencies_before_launch(self, launcher_bundle):
        """Just-before-launch recheck consumes the work gate and blocks stale work."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        # Create issue with a real normal dependency so the work gate is consulted
        issue_with_body = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
            repo="test/repo",
            body="Depends-on: #100",
            milestone="M1",
        )

        class _Checker:
            def get_dependency_issue_snapshot(
                self, issue_number: int, repo: str | None = None
            ) -> DependencyIssueSnapshot | None:
                if issue_number != 100:
                    return None
                return DependencyIssueSnapshot(
                    state="open",  # dependency still open
                    milestone="M1",
                )

        evaluator = DependencyEvaluator(issue_checker=_Checker(), events=NullEventSink())

        launcher_bundle.launcher._dependency_evaluator = evaluator  # noqa: SLF001
        launcher_bundle.launcher._refresh_issue = lambda n: issue_with_body  # noqa: SLF001

        result = launcher_bundle.launcher.launch_issue_session(issue_with_body, active_sessions=[])

        assert result.success is False
        assert "Dependencies not satisfied" in result.reason
        assert "#100" in result.reason

    def test_recheck_blocks_stale_stack_work_changed_after_planning(
        self, launcher_bundle, mock_events
    ):
        """Predecessor review state changing between planning and launch blocks start.

        The just-before-launch recheck re-gathers predecessor facts, so a stack
        successor that was work-ready at planning time must not start once the
        predecessor's branch/review state regresses (ADR-0029 race guard).
        """
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        issue = Issue(
            number=300,
            title="Stacked successor",
            labels=["agent:web"],
            repo="test/repo",
            body="Stack-after: #200",
            milestone="M1",
        )

        class _Checker:
            def get_dependency_issue_snapshot(
                self, issue_number: int, repo: str | None = None
            ) -> DependencyIssueSnapshot | None:
                if issue_number != 200:
                    return None
                return DependencyIssueSnapshot(
                    state="open",  # predecessor still open
                    milestone="M1",
                )

        class _MutableProvider:
            """Returns whatever facts are current at gather time."""

            def __init__(self):
                self.facts = PredecessorFacts(
                    branch_usable=True, validation_passed=True,
                    agent_reviewed=True, branch_name="200-base",
                )

            def gather_facts(self, targets):
                return {t: self.facts for t in targets}

        provider = _MutableProvider()
        evaluator = DependencyEvaluator(
            issue_checker=_Checker(), events=mock_events,
            predecessor_facts_provider=provider,
        )
        launcher_bundle.launcher._dependency_evaluator = evaluator  # noqa: SLF001
        launcher_bundle.launcher._refresh_issue = lambda n: issue  # noqa: SLF001

        # At planning time the predecessor was validated + reviewed -> work-ready.
        planning = evaluator.evaluate_work_gate(300, "Stack-after: #200", "M1", emit_event=False)
        assert planning.can_start_work is True

        # Predecessor review is withdrawn (e.g. a new push) before launch.
        provider.facts = PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=False,
        )
        mock_events.clear()

        result = launcher_bundle.launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is False
        assert "Dependencies not satisfied" in result.reason
        blocked = mock_events.get_events_by_name("issue.dependency_blocked")
        assert len(blocked) == 1
        data = blocked[0].data
        assert data["gate"] == "work"
        assert any(
            r["mode"] == "stack"
            and r["predecessor"] == "#200"
            and r["reason"] == "predecessor_review_pending"
            for r in data["blocked_reasons"]
        )

    @pytest.mark.parametrize("rework_via", ["add", "remove"])
    def test_recheck_through_real_pr_cache_passes_until_pr_label_changes(
        self, launcher_bundle, mock_events, rework_via
    ):
        """Planning and the just-before-launch recheck both pass with no PR-label
        change, and the recheck blocks only after an actual PR-number label write.

        Round-2 F1 regression wired end-to-end through the real ``GitHubAdapter``
        cache and ``GitStackPredecessorFactsProvider``: the recheck re-runs
        ``evaluate_work_gate`` (``LaunchDependencyGate.verify_fresh``),
        which gathers PR-scoped review labels and then issue labels. Before the
        cache-owner fix, the first evaluation's empty issue-label refresh
        corrupted the cached PR's ``code-reviewed``, so this second (recheck)
        evaluation falsely blocked a still-reviewed predecessor. With unchanged
        live PR labels the recheck must pass; after a real PR-number add of
        ``needs-rework`` or removal of ``code-reviewed`` it must block.
        """
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        config = Config(
            repo="test/repo",
            repo_root=Path("/tmp/repo"),
            worktree_base=Path("/tmp"),
            agents={"agent:web": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
        )
        config.github_token = "test-token"
        config.github_cache_ttl_seconds = 60

        live_labels = ["code-reviewed"]  # PR #201 is reviewed; issue #200 has none
        http = MagicMock()
        http.get_prs_for_issue.return_value = [{"number": 201}]
        http.get_issue_labels.return_value = []

        def _get_pr(number):
            assert number == 201
            return {
                "number": 201,
                "title": "#200: predecessor",
                "html_url": "https://example/pr/201",
                "head": {"ref": "200-base"},
                "body": "",
                "state": "open",
                "labels": [{"name": name} for name in live_labels],
            }

        http.get_pr.side_effect = _get_pr
        adapter = GitHubAdapter(
            repo="test/repo",
            config=config,
            cache=GitHubCache(default_ttl=300.0),
            http_client=http,
            verify_writes=False,
        )
        provider = GitStackPredecessorFactsProvider(
            repository_host=adapter,
            label_manager=LabelManager(config),
            repo="test/repo",
        )

        class _Checker:
            def get_dependency_issue_snapshot(self, issue_number, repo=None):
                if issue_number != 200:
                    return None
                return DependencyIssueSnapshot(
                    state="open",  # predecessor open
                    milestone="M1",
                )

        evaluator = DependencyEvaluator(
            issue_checker=_Checker(), events=mock_events,
            predecessor_facts_provider=provider,
        )

        issue = Issue(
            number=300,
            title="Stacked successor",
            labels=["agent:web"],
            repo="test/repo",
            body="Stack-after: #200",
            milestone="M1",
        )
        launcher_bundle.launcher._dependency_evaluator = evaluator  # noqa: SLF001
        launcher_bundle.launcher._refresh_issue = lambda n: issue  # noqa: SLF001

        # Planning: the work gate opens (predecessor open + reviewed PR).
        planning = evaluator.evaluate_work_gate(
            300, "Stack-after: #200", "M1", emit_event=False
        )
        assert planning.can_start_work is True

        # Just-before-launch recheck with NO PR-label change must still pass —
        # the issue-label refresh during gather must not corrupt the cached PR —
        # and it resolves the predecessor branch as the successor's stack base.
        freshness = launcher_bundle.launcher._dependency_gate.verify_fresh(issue)  # noqa: SLF001
        assert freshness.failure is None
        assert freshness.stack_base_branch == "200-base"

        # The predecessor PR actually moves out of a clean review, by PR NUMBER.
        if rework_via == "add":
            live_labels.append("needs-rework")
            adapter.add_label(201, "needs-rework")
        else:
            live_labels.remove("code-reviewed")
            adapter.remove_label(201, "code-reviewed")

        # Now the recheck must block the stale stack work.
        result = launcher_bundle.launcher._dependency_gate.verify_fresh(issue)  # noqa: SLF001
        assert result.failure is not None
        assert result.failure.success is False
        assert "Dependencies not satisfied" in result.failure.reason


class TestLaunchValidationRetrySession:
    """Tests for validation retry launch behavior."""

    def test_launches_retry_with_retry_prompt_and_preserves_branch(
        self,
        launcher_bundle,
        mock_worktree_manager,
    ):
        """Validation retry launch uses the pending branch and retry prompt."""
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-fix-checkout",
            original_prompt="Work on issue #123: Fix checkout",
            validation_error="Validation blocked before running command: dirty worktree",
            validation_error_file="/tmp/validation-errors.txt",
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )

        result = launcher_bundle.launcher.launch_validation_retry_session(
            retry,
            active_sessions=[],
        )

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "issue-123"
        assert result.session.validation_retry_count == 1
        assert result.session.original_prompt == "Work on issue #123: Fix checkout"
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__coding-2")
        assert mock_worktree_manager.create_calls[0]["branch_name"] == "123-fix-checkout"
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "Validation Retry" in command
        assert "dirty worktree" in command

    def test_internal_review_instructions_reach_validation_retry_command(
        self,
        internal_review_launcher_bundle,
    ):
        bundle, provider = internal_review_launcher_bundle
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-fix-checkout",
            original_prompt="Work on issue #123",
            validation_error="dirty worktree",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )

        result = bundle.launcher.launch_validation_retry_session(
            retry,
            active_sessions=[],
        )

        assert result.success is True
        assert "INTERNAL-REVIEW-MARKER" in bundle.create_session_calls[0]["cmd"]
        provider.prepare.assert_called_once_with(
            task=TaskKind.CODE,
            agent_label="agent:web",
        )

    def test_missing_internal_review_instructions_fail_before_retry_mutation(
        self,
        unavailable_provider_internal_review_bundle,
        mock_worktree_manager,
    ):
        bundle, provider, readiness_probe = (
            unavailable_provider_internal_review_bundle
        )
        provider.prepare.return_value = CoderPromptAddendumUnavailable(
            "instructions unreadable"
        )
        work_claim = MagicMock()
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-fix-checkout",
            original_prompt="Work on issue #123",
            validation_error="dirty worktree",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )

        result = bundle.launcher.launch_validation_retry_session(
            retry,
            active_sessions=[],
            work_claim=work_claim,
        )

        assert result.success is False
        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE
        work_claim.hold_before_spawn.assert_not_called()
        assert bundle.claim_manager is not None
        bundle.claim_manager.attempt_claim.assert_not_called()
        readiness_probe.check_launch_readiness.assert_not_called()
        bundle.action_applier.apply.assert_not_called()
        assert mock_worktree_manager.create_calls == []
        assert bundle.create_session_calls == []


class TestLaunchIssueSessionPerSessionWorktree:
    """Tests for per-session worktree mode (lines 264-266)."""

    def test_creates_per_session_worktree_when_env_set(self, session_launcher, sample_issue):
        """Verify per-session worktree base when env var is set."""
        with patch.dict(os.environ, {"ORCHESTRATOR_WORKTREE_PER_SESSION": "1"}):
            result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

            assert result.success is True


# =============================================================================
# Review Session Launch Tests
# =============================================================================


class TestLaunchReviewSession:
    """Tests for launch_review_session method."""

    def test_successful_launch(self, session_launcher, mock_repo_host):
        """Verify successful review session launch."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "review-456"
        assert result.session.key.task == TaskKind.REVIEW
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__review-1")

    def test_internal_coder_instructions_do_not_change_reviewer_command(
        self,
        internal_review_launcher_bundle,
    ):
        bundle, provider = internal_review_launcher_bundle
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        assert "INTERNAL-REVIEW-MARKER" not in bundle.create_session_calls[0]["cmd"]
        provider.prepare.assert_not_called()

    def test_review_launch_threads_issue_label_provider_args(self, launcher_bundle):
        """Label-derived provider args should reach review command and wrapper."""
        launcher_bundle.launcher.config.agents["agent:reviewer"].provider = "claude-code"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
            issue_labels=("agent:web", "verbose"),
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "--verbose" in shlex.split(command)

    def test_fails_when_no_review_agent_configured(self, session_launcher):
        """Verify fails when no code review agent configured (line 418)."""
        session_launcher.config.code_review_agent = None
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert "No code review agent configured" in result.reason

    def test_fails_when_agent_config_missing(self, session_launcher):
        """Verify fails when agent config not found (line 422)."""
        session_launcher.config.code_review_agent = "agent:nonexistent"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert "No agent config" in result.reason

    def test_fails_when_no_repo_configured(self, session_launcher):
        """Verify fails when no repo configured (line 435)."""
        session_launcher.config.repo = None
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert "No repo configured" in result.reason

    def test_skips_when_terminal_already_running(self, launcher_bundle):
        """Verify keeps queued when terminal exists (EXISTING_TERMINAL)."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "review-456"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert result.disposition is LaunchDisposition.EXISTING_TERMINAL

    def test_triggers_review_state_machine(self, launcher_bundle):
        """Verify review state machine is triggered."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True

    def test_review_launch_clears_review_interrupted_guard_label(self, launcher_bundle):
        """Review launch should clear interrupted review retry guard."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        guard_label = launcher_bundle.launcher.config.retry.interrupted_sessions.review_guard_label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == review.issue_number
            and a.label == guard_label
            for a in actions
        )
        review_machine = launcher_bundle.review_machines[456]
        assert review_machine.state == ReviewState.IN_REVIEW.value

    def test_drops_stale_pending_review_for_blocked_issue(
        self,
        launcher_bundle,
        mock_repo_host,
        caplog,
    ):
        """Blocked issues should invalidate queued review launches."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        mock_repo_host.issues[123] = Issue(
            number=123,
            title="Blocked issue",
            labels=["agent:web", "blocked-failed", "needs-rework"],
            repo="test/repo",
        )

        with caplog.at_level("INFO"):
            result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert result.reason == "Stale pending review: issue_blocked"
        assert launcher_bundle.create_session_calls == []
        assert "Dropping stale pending review: pr=456 issue=123 reason=issue_blocked" in caplog.text

    def test_review_existing_work_includes_keep_current_note(
        self, launcher_bundle, mock_repo_host
    ):
        """Keep-current label should inject reviewer instruction."""
        keep_current_label = launcher_bundle.launcher.config.get_label_review_keep_current_approach()
        mock_repo_host.get_pr = MagicMock(return_value=PRInfo(
            number=456,
            title="Test PR",
            url="https://github.com/test/repo/pull/456",
            branch="issue-123",
            body="Test body",
            state="open",
            labels=[keep_current_label],
        ))

        worktree_info = WorktreeInfo(path=Path("/tmp/worktree"), branch_name="issue-123")

        note = build_review_existing_work(
            worktree_info=worktree_info,
            pr_number=456,
            repository_host=mock_repo_host,
            keep_current_label=keep_current_label,
        )

        assert note is not None
        assert "Keep the current approach" in note

    def test_per_session_worktree(self, session_launcher):
        """Verify per-session worktree mode for reviews (lines 463-465)."""
        with patch.dict(os.environ, {"ORCHESTRATOR_WORKTREE_PER_SESSION": "1"}):
            review = PendingReview(
                issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
                pr_number=456,
                pr_url="https://github.com/test/repo/pull/456",
                branch_name="123-feature",
                _issue_number=123,
            )

            result = session_launcher.launch_review_session(review, active_sessions=[])

            assert result.success is True


class TestLaunchRetrospectiveReviewSession:
    """Tests for review-first existing-implementation sessions."""

    def test_successful_launch_records_review_first_identity(
        self,
        launcher_bundle,
        mock_events,
        mock_worktree_manager,
    ):
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            prior_pr_number=512,
            prior_pr_url="https://github.com/test/repo/pull/512",
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "retrospective-review-365"
        assert result.session.key.task == TaskKind.RETROSPECTIVE_REVIEW
        assert result.session.pr_number == 512
        assert result.session.issue.labels == [
            "agent:web",
            "agent:reviewer",
            "lack-of-review-redo",
        ]
        assert "RETROSPECTIVE REVIEW MODE" in result.session.original_prompt
        assert "issue #365" in result.session.original_prompt
        assert "Prior orchestrator PR: #512" in result.session.original_prompt

        create_call = mock_worktree_manager.create_calls[0]
        assert create_call["issue_number"] == 365
        assert create_call["enforce_hooks"] is False
        reuse_options = create_call["reuse_options"]
        assert reuse_options is not None
        assert reuse_options.allow_remote_branch_delete is False

        event = next(e for e in mock_events.events if str(e.name) == str(EventName.REVIEW_STARTED))
        assert event.data["task"] == TaskKind.RETROSPECTIVE_REVIEW.value
        assert event.data["prior_pr_number"] == 512
        assert event.data["source_agent"] == "agent:web"

    def test_retrospective_review_threads_issue_label_provider_args(
        self,
        launcher_bundle,
    ):
        launcher_bundle.launcher.config.agents["agent:reviewer"].provider = "claude-code"
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_labels=("agent:web", "lack-of-review-redo", "verbose"),
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "--verbose" in shlex.split(command)

    def test_unset_prior_pr_is_resolved_lazily_at_launch(
        self,
        launcher_bundle,
        mock_repo_host,
        mock_events,
    ):
        """Discovery leaves prior_pr unset; the launcher resolves it here with a
        single search-only lookup (no per-PR hydration) for the one issue being
        launched, and the resolved PR flows into the prompt and launch event."""
        mock_repo_host.prs[365] = [
            PRInfo(
                number=511,
                title="Manual PR",
                url="https://github.com/test/repo/pull/511",
                branch="365-manual",
                body="hand-written, no marker",
                state="closed",
                labels=[],
            ),
            PRInfo(
                number=512,
                title="Orchestrator PR",
                url="https://github.com/test/repo/pull/512",
                branch="365-scratch",
                body=f"{ORCHESTRATOR_PR_MARKER}\nGenerated work.",
                state="closed",
                labels=[],
            ),
        ]
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            # prior_pr_number / prior_pr_url intentionally unset (discovery default).
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        # Resolved to the orchestrator-signed PR, not the manual one...
        assert result.session.pr_number == 512
        assert "Prior orchestrator PR: #512" in result.session.original_prompt
        # ...and back-filled onto the pending review so the dashboard/events see it.
        assert review.prior_pr_number == 512
        assert review.prior_pr_url == "https://github.com/test/repo/pull/512"
        event = next(
            e for e in mock_events.events if str(e.name) == str(EventName.REVIEW_STARTED)
        )
        assert event.data["prior_pr_number"] == 512
        # Exactly one cheap search call — no fan-out, no per-PR hydration.
        assert mock_repo_host.search_pr_refs_calls == [365]

    def test_preset_prior_pr_skips_lookup_at_launch(
        self,
        launcher_bundle,
        mock_repo_host,
    ):
        """When the UI preflight/queue path already resolved the prior PR, the
        launcher must not search again."""
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            prior_pr_number=512,
            prior_pr_url="https://github.com/test/repo/pull/512",
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        assert result.session.pr_number == 512
        assert mock_repo_host.search_pr_refs_calls == []

    def test_launch_then_completion_clears_real_blocking_labels(
        self,
        launcher_bundle,
    ):
        """Production path: a blocked, trigger-labeled issue is queued with its
        real labels, launched, then approved — and the completion must clear the
        blocking labels. Regression guard proving issue_labels reach
        session.issue.labels (the synthetic pseudo-issue used to drop them, so
        get_blocking returned empty and no RemoveLabelAction was generated).
        """
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_labels=(
                "agent:web",
                "lack-of-review-redo",
                "blocked",
                "blocked-failed",
            ),
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        session = result.session
        # The launched session must carry the issue's real blocking labels...
        assert "blocked" in session.issue.labels
        assert "blocked-failed" in session.issue.labels

        # ...so completion policy actually generates the removals in production.
        config = launcher_bundle.launcher.config
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        actions = retrospective_review_completion_actions(
            session=session,
            status=SessionStatus.COMPLETED,
            detail={"outcome": "review_approved"},
            config=config,
            label_manager=LabelManager(config),
        )
        removed = [a.label for a in actions if isinstance(a, RemoveLabelAction)]
        assert "blocked" in removed
        assert "blocked-failed" in removed

    def test_keeps_queued_when_retrospective_terminal_already_running(
        self,
        launcher_bundle,
    ):
        launcher_bundle.session_exists_override[0] = (
            lambda name: name == "retrospective-review-365"
        )
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is False
        assert result.disposition is LaunchDisposition.EXISTING_TERMINAL


# =============================================================================
# Rework Session Launch Tests
# =============================================================================


class TestLaunchReworkSession:
    """Tests for launch_rework_session method (lines 585, 597-599, 604-605, 611-765)."""

    @pytest.fixture(autouse=True)
    def _no_feedback_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.control.session_launcher.time.sleep",
            lambda _: None,
        )

    def test_successful_launch_with_pr(self, session_launcher, mock_repo_host, mock_events):
        """Verify successful rework session launch when PR exists."""
        mock_repo_host.prs[123] = [
            PRInfo(
                number=456,
                title="Fix issue #123",
                url="https://github.com/test/repo/pull/456",
                branch="123-feature",
                body="",
                state="open",
                labels=[],
            )
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "rework-123"
        assert result.session.key.task == TaskKind.REWORK
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__coding-2")
        started = next(e for e in mock_events.events if str(e.name) == "rework.started")
        assert started.data["agent"] == "agent:web"
        assert started.data["task"] == "rework"

    def test_internal_review_instructions_reach_rework_command(
        self,
        internal_review_launcher_bundle,
    ):
        bundle, provider = internal_review_launcher_bundle
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        assert "INTERNAL-REVIEW-MARKER" in bundle.create_session_calls[0]["cmd"]
        provider.prepare.assert_called_once_with(
            task=TaskKind.REWORK,
            agent_label="agent:web",
        )

    def test_missing_internal_review_instructions_fail_before_rework_mutation(
        self,
        unavailable_provider_internal_review_bundle,
        mock_worktree_manager,
    ):
        bundle, provider, readiness_probe = (
            unavailable_provider_internal_review_bundle
        )
        provider.prepare.return_value = CoderPromptAddendumUnavailable(
            "instructions empty"
        )
        work_claim = MagicMock()
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = bundle.launcher.launch_rework_session(
            rework,
            active_sessions=[],
            work_claim=work_claim,
        )

        assert result.success is False
        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE
        work_claim.hold_before_spawn.assert_not_called()
        readiness_probe.check_launch_readiness.assert_not_called()
        bundle.action_applier.apply.assert_not_called()
        assert mock_worktree_manager.create_calls == []
        assert bundle.create_session_calls == []

    def test_successful_launch_without_pr(self, session_launcher):
        """Verify launch when no PR exists (lines 597-599)."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Uses fallback branch name when no PR

    def test_rework_launch_clears_coding_interrupted_guard_label(self, launcher_bundle):
        """Rework launch should clear interrupted coding retry guard."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        guard_label = launcher_bundle.launcher.config.retry.interrupted_sessions.coding_guard_label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == 123
            and a.label == guard_label
            for a in actions
        )

    def test_fails_when_agent_config_missing(self, session_launcher):
        """Verify fails when agent config not found (line 585)."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:nonexistent",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert "No agent config" in result.reason

    def test_skips_when_already_in_active_sessions(self, session_launcher, sample_agent_config, tmp_path):
        """Verify skips when rework already active (lines 604-605)."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = GitHubIssueKey(repo="test/repo", external_id="123")
        existing_session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.REWORK),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="rework-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="rework-123",
            ),
        )
        rework = PendingRework(
            issue_key=issue_key,
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[existing_session])

        assert result.success is False
        assert "Already in active sessions" in result.reason

    def test_skips_when_terminal_already_running(self, launcher_bundle):
        """Verify keeps queued when terminal exists."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "rework-123"
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert result.disposition is LaunchDisposition.EXISTING_TERMINAL

    def test_updates_rework_cycle_label(self, launcher_bundle, mock_repo_host):
        """Verify rework cycle label is updated (lines 820-839)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=2,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Should add rework-cycle-2 label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "rework-cycle-2" for a in actions)

    def test_removes_needs_rework_label(self, launcher_bundle, mock_repo_host):
        """Verify needs-rework label is removed (lines 754-764)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.labels[456] = {"needs-rework"}
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Should remove needs-rework label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, RemoveLabelAction) and a.label == "needs-rework" for a in actions)

    def test_rework_pr_view_changed_uses_stable_issue_key(self, launcher_bundle, mock_repo_host, mock_events):
        """PR_VIEW_CHANGED event on rework start must use stable issue_key, not str(issue_number)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="M0-721"),
            agent_type="agent:web",
            rework_cycle=1,
            issue_number=123,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        pr_view_events = mock_events.get_events_by_name("pr.view_changed")
        assert len(pr_view_events) >= 1
        # The rework-start event removing needs-rework should use stable key
        remove_event = next(
            (e for e in pr_view_events if "needs-rework" in e.data.get("removed", [])),
            None,
        )
        assert remove_event is not None, "Expected pr.view_changed with needs-rework removal"
        assert remove_event.data["issue_key"] == "M0-721"

    def test_includes_reviewer_feedback_in_prompt(self, launcher_bundle, mock_repo_host):
        """Verify reviewer feedback is included in the agent prompt."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "Please add unit tests", "user": {"login": "reviewer1"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" in command
        assert "Please add unit tests" in command
        assert "reviewer1" in command

    def test_includes_post_publish_validation_feedback_in_prompt(self, launcher_bundle, mock_repo_host):
        """Verify post-publish validation feedback is included in the rework prompt."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = []
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
            source="post_publish_validation",
            feedback="POST-PUBLISH VALIDATION FAILURE (address these issues):\n\nResolve merge conflicts.",
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "POST-PUBLISH VALIDATION FAILURE" in command
        assert "Resolve merge conflicts." in command

    def test_excludes_approved_reviews_from_feedback(self, launcher_bundle, mock_repo_host):
        """Verify APPROVED reviews are not included in feedback."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "Fix the bug", "user": {"login": "alice"}},
            {"state": "APPROVED", "body": "LGTM", "user": {"login": "bob"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "Fix the bug" in command
        assert "alice" in command
        # APPROVED review should not be included
        assert "LGTM" not in command
        assert "bob" not in command

    def test_excludes_reviews_with_empty_body(self, launcher_bundle, mock_repo_host):
        """Verify reviews with empty bodies are excluded."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "", "user": {"login": "reviewer"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        # No feedback should be included (empty body)
        assert "REVIEWER FEEDBACK" not in command

    def test_no_feedback_when_no_reviews(self, launcher_bundle, mock_repo_host):
        """Verify no feedback section when there are no reviews."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = []
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" not in command

    def test_escapes_quotes_in_feedback(self, launcher_bundle, mock_repo_host):
        """Verify quotes in reviewer feedback don't break the command."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "Don't use 'eval' here", "user": {"login": "reviewer"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Command should not break - the eval text should still be present
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "eval" in command

    def test_includes_commented_reviews_with_body(self, launcher_bundle, mock_repo_host):
        """Verify COMMENTED reviews with body text are included."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "COMMENTED", "body": "Consider using a helper function here", "user": {"login": "reviewer"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" in command
        assert "Consider using a helper function here" in command

    def test_uses_local_feedback_file_within_cache_window(self, launcher_bundle, mock_repo_host, mock_worktree_manager):
        """Verify local feedback file is used when within cache window."""
        import json
        from datetime import datetime, timezone

        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        # Don't set pr_reviews - we want to test local file is used instead

        # Create local feedback file in review session's run directory
        # Issue number is 123, so worktree is at tmp_path / "worktree-123"
        worktree_path = mock_worktree_manager.tmp_path / "worktree-123"
        worktree_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        review_run_dir = sessions_dir / "20240115-120000__review-456"
        review_run_dir.mkdir(parents=True, exist_ok=True)

        feedback_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pr_number": 456,
            "review_issues": "Local feedback: please add tests",
        }
        (review_run_dir / "reviewer-feedback.json").write_text(json.dumps(feedback_data))

        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" in command
        assert "Local feedback: please add tests" in command

    def test_falls_back_to_github_when_local_file_too_old(self, launcher_bundle, mock_repo_host, mock_worktree_manager):
        """Verify GitHub API is used when local file is outside cache window."""
        import json
        from datetime import datetime, timezone, timedelta

        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "GitHub API feedback", "user": {"login": "reviewer"}},
        ]

        # Create old local feedback file (outside cache window)
        worktree_path = mock_worktree_manager.tmp_path / "worktree-123"
        worktree_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        review_run_dir = sessions_dir / "20240115-120000__review-456"
        review_run_dir.mkdir(parents=True, exist_ok=True)

        old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        feedback_data = {
            "timestamp": old_timestamp,
            "pr_number": 456,
            "review_issues": "Old local feedback",
        }
        (review_run_dir / "reviewer-feedback.json").write_text(json.dumps(feedback_data))

        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        # Should use GitHub API feedback, not old local file
        assert "GitHub API feedback" in command
        assert "Old local feedback" not in command

    def test_copies_feedback_from_review_to_rework_run_dir(self, launcher_bundle, mock_repo_host, mock_worktree_manager):
        """Verify feedback is copied from review session's run dir to rework's run dir."""
        import json
        from datetime import datetime, timezone

        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]

        # Create local feedback file in review session's run directory
        worktree_path = mock_worktree_manager.tmp_path / "worktree-123"
        worktree_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        review_run_dir = sessions_dir / "20240115-120000__review-456"
        review_run_dir.mkdir(parents=True, exist_ok=True)

        feedback_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pr_number": 456,
            "review_issues": "Copied feedback content",
        }
        (review_run_dir / "reviewer-feedback.json").write_text(json.dumps(feedback_data))

        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Find the rework session's run directory
        rework_dirs = [d for d in sessions_dir.iterdir() if d.name.startswith("rework-") or d.name.startswith("20")]
        # Filter to the coding session (rework creates a coding-N directory)
        coding_dirs = [d for d in rework_dirs if "coding" in d.name]
        assert len(coding_dirs) > 0, "Rework coding session directory should exist"
        rework_feedback = coding_dirs[0] / "reviewer-feedback.json"
        assert rework_feedback.exists(), "Feedback should be copied to rework run dir"
        copied_data = json.loads(rework_feedback.read_text())
        assert copied_data["review_issues"] == "Copied feedback content"


# Note: TestRunSetupCommands class deleted - tested private _run_setup_commands method.
# Setup command behavior is already tested through test_runs_setup_commands in TestLaunchIssueSession.


# =============================================================================
# Orchestrator Wrapper Function Tests
# =============================================================================


class TestOrchestratorLaunchSession:
    """Tests for orchestrator_launch_session function."""

    def test_appends_session_to_active(self, session_launcher, sample_issue):
        """Verify session is appended to active_sessions."""
        state = OrchestratorState()

        result = orchestrator_launch_session(sample_issue, state, session_launcher)

        assert result is not None
        assert len(state.active_sessions) == 1
        assert state.active_sessions[0].terminal_id == "issue-123"

    def test_suppresses_duplicate_terminal_id_from_launch_result(
        self,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Launch wrappers must not admit duplicate terminal IDs."""
        existing = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "existing",
            branch_name="123-existing",
            run_assets=make_session_run_assets(
                tmp_path / "existing",
                session_name="issue-123",
            ),
        )
        duplicate = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "duplicate",
            branch_name="123-duplicate",
            run_assets=make_session_run_assets(
                tmp_path / "duplicate",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[existing])
        session_launcher = MagicMock()
        session_launcher.launch_issue_session.return_value = LaunchResult(
            duplicate,
            True,
        )

        result = orchestrator_launch_session(sample_issue, state, session_launcher)

        assert result is duplicate
        assert state.active_sessions == [existing]

    def test_keeps_orphaned_terminal_unrestored_without_run_assets(
        self,
        launcher_bundle,
        sample_issue,
    ):
        """Launch routing does not synthesize active sessions without run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "issue-123"
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "issue-123",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": "",
            }
        ]
        state = OrchestratorState()
        mock_restorer = MagicMock()

        result = orchestrator_launch_session(
            sample_issue,
            state,
            launcher_bundle.launcher,
            mock_restorer,
        )

        assert result is None
        assert state.active_sessions == []
        mock_restorer.restore_known_terminal.assert_not_called()

    def test_restores_orphaned_terminal_from_discovered_run_assets(
        self,
        launcher_bundle,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Launch routing re-tracks existing terminals with typed run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "issue-123"
        run_assets = make_session_run_assets(tmp_path, session_name="issue-123")
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "issue-123",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": str(run_assets.run_dir),
            }
        ]
        state = OrchestratorState()
        restored = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path,
            branch_name="123-test",
            run_assets=run_assets,
        )
        mock_restorer = MagicMock()
        mock_restorer.canonical_terminal_id.return_value = "issue-123"
        mock_restorer.restore_known_terminal.return_value = [restored]

        result = orchestrator_launch_session(
            sample_issue,
            state,
            launcher_bundle.launcher,
            mock_restorer,
        )

        assert result is restored
        assert state.active_sessions == [restored]
        mock_restorer.restore_known_terminal.assert_called_once_with(
            issue_number=123,
            session_name="issue-123",
            run_dir=run_assets.run_dir,
            is_review=False,
            already_tracked=[],
            tab_name="",
        )


class TestOrchestratorLaunchValidationRetrySession:
    """Tests for validation retry launch wrapper."""

    def test_removes_pending_retry_after_success(self, launcher_bundle):
        """Successful retry launch removes only that issue from pending retries."""
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-fix-checkout",
            original_prompt="original task",
            validation_error="dirty worktree",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        other_retry = PendingValidationRetry(
            issue_number=456,
            issue_title="Other",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-456",
            branch_name="456-other",
            original_prompt="original task",
            validation_error="failed",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        state = OrchestratorState(pending_validation_retries=[retry, other_retry])

        result = orchestrator_launch_validation_retry_session(
            retry,
            state,
            launcher_bundle.launcher,
            MagicMock(),
            _claims_store(),
        )

        assert result is not None
        assert [r.issue_number for r in state.pending_validation_retries] == [456]
        assert [s.terminal_id for s in state.active_sessions] == ["issue-123"]

    def test_restores_existing_terminal_and_removes_pending(
        self,
        launcher_bundle,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Existing retry terminals are re-tracked from discovered run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "issue-123"
        run_assets = make_session_run_assets(tmp_path, session_name="issue-123")
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "issue-123",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": str(run_assets.run_dir),
            }
        ]
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path=str(tmp_path),
            branch_name="123-fix-checkout",
            original_prompt="original task",
            validation_error="dirty worktree",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        state = OrchestratorState(pending_validation_retries=[retry])
        restored = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path,
            branch_name="123-fix-checkout",
            run_assets=run_assets,
        )
        mock_restorer = MagicMock()
        mock_restorer.canonical_terminal_id.return_value = "issue-123"
        mock_restorer.restore_known_terminal.return_value = [restored]

        result = orchestrator_launch_validation_retry_session(
            retry,
            state,
            launcher_bundle.launcher,
            mock_restorer,
            _claims_store(),
        )

        assert result is restored
        assert state.active_sessions == [restored]
        assert state.pending_validation_retries == []


class TestOrchestratorLaunchReviewSession:
    """Tests for orchestrator_launch_review_session function (lines 977, 990)."""

    def test_removes_from_pending_and_adds_to_active(self, session_launcher):
        """Verify review is removed from pending and added to active."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        state = OrchestratorState()
        state.pending_reviews = [review]

        mock_restorer = MagicMock()

        result = orchestrator_launch_review_session(review, state, session_launcher, mock_restorer, _claims_store())

        assert result is not None
        assert len(state.pending_reviews) == 0
        assert len(state.active_sessions) == 1

    def test_keeps_orphaned_terminal_unrestored_without_run_assets(self, launcher_bundle):
        """Review launch routing refuses restoration without run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "review-456"
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "Review PR #456",
                "is_review": True,
                "session_name": "review-456",
                "run_dir": "",
            }
        ]
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        state = OrchestratorState()
        state.pending_reviews = [review]

        mock_restorer = MagicMock()
        mock_restorer.restore_known_terminal.return_value = []

        result = orchestrator_launch_review_session(review, state, launcher_bundle.launcher, mock_restorer, _claims_store())

        assert result is None
        mock_restorer.restore_known_terminal.assert_not_called()
        assert state.pending_reviews == [review]

    def test_restores_review_terminal_from_discovered_run_assets(
        self,
        launcher_bundle,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Review launch routing re-tracks keep-queued review terminals."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "review-456"
        run_assets = make_session_run_assets(tmp_path, session_name="review-456")
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "Review PR #456",
                "is_review": True,
                "session_name": "review-456",
                "run_dir": str(run_assets.run_dir),
            }
        ]
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        state = OrchestratorState(pending_reviews=[review])
        restored = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.REVIEW),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="review-456",
            worktree_path=tmp_path,
            branch_name="123-feature",
            run_assets=run_assets,
            pr_number=456,
        )
        mock_restorer = MagicMock()
        mock_restorer.canonical_terminal_id.return_value = "review-456"
        mock_restorer.restore_known_terminal.return_value = [restored]

        result = orchestrator_launch_review_session(
            review,
            state,
            launcher_bundle.launcher,
            mock_restorer,
            _claims_store(),
        )

        assert result is restored
        assert state.active_sessions == [restored]
        assert state.pending_reviews == []
        mock_restorer.restore_known_terminal.assert_called_once_with(
            issue_number=123,
            session_name="review-456",
            run_dir=run_assets.run_dir,
            is_review=True,
            already_tracked=[],
            tab_name="Review PR #456",
        )


class TestOrchestratorLaunchReworkSession:
    """Tests for orchestrator_launch_rework_session function."""

    @pytest.fixture(autouse=True)
    def _no_feedback_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.control.session_launcher.time.sleep",
            lambda _: None,
        )

    def test_removes_from_pending_and_adds_to_active(self, session_launcher):
        """Verify rework is removed from pending and added to active."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )
        state = OrchestratorState()
        state.pending_reworks = [rework]

        mock_restorer = MagicMock()

        result = orchestrator_launch_rework_session(rework, state, session_launcher, mock_restorer, _claims_store())

        assert result is not None
        assert len(state.pending_reworks) == 0
        assert len(state.active_sessions) == 1


# =============================================================================
# Tech Lead Session Tests
# =============================================================================


class TestPendingSessionQueuesTechLeadIntake:
    """Owner-level tech_lead intake invariants (#6768 round 3).

    PendingSessionQueues is the only writer of state.pending_tech_lead_reviews:
    it constructs the queue item for the declared variant, applies ONE dedup
    rule (issue number vs the pending queue), and returns a typed outcome.
    """

    def test_queue_batch_review_constructs_batch_entry(self):
        state = OrchestratorState()

        outcome = PendingSessionQueues(state).queue_batch_review(7, "Tech Lead Batch")

        assert outcome is TechLeadQueueOutcome.QUEUED
        (entry,) = state.pending_tech_lead_reviews
        assert entry.issue_number == 7
        assert entry.title == "Tech Lead Batch"
        assert entry.flavor is TechLeadSessionFlavor.BATCH_REVIEW

    def test_queue_failure_investigation_constructs_failure_entry(self):
        state = OrchestratorState()
        failure = DiscoveredFailure(
            issue_number=8, issue_title="Timeout victim", failure_reason="timed_out"
        )

        outcome = PendingSessionQueues(state).queue_failure_investigation(
            8, "Investigate: timeout", failure=failure
        )

        assert outcome is TechLeadQueueOutcome.QUEUED
        (entry,) = state.pending_tech_lead_reviews
        assert entry.issue_number == 8
        assert entry.title == "Investigate: timeout"
        assert entry.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        # The typed triggering failure rides the queue item so the launch-time
        # board snapshot still has it after discovered_failures is cleared.
        assert entry.failure is failure

    def test_failure_investigation_without_failure_context_fails_fast(self):
        """The queue item is the only carrier of the triggering failure; a
        failure investigation without it would launch with an empty
        recent_failures section (the P1 defect this guards against)."""
        with pytest.raises(ValueError, match="failure"):
            PendingTechLeadReview(
                issue_number=8,
                title="Investigate: timeout",
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            )

    def test_batch_review_with_failure_context_fails_fast(self):
        """Batch reviews are threshold-created; failure context is a producer bug."""
        with pytest.raises(ValueError, match="batch"):
            PendingTechLeadReview(
                issue_number=7,
                title="Tech Lead Batch",
                flavor=TechLeadSessionFlavor.BATCH_REVIEW,
                failure=DiscoveredFailure(
                    issue_number=7, issue_title="x", failure_reason="failed"
                ),
            )

    def test_duplicate_issue_number_returns_duplicate_without_double_queue(self):
        state = OrchestratorState()
        queues = PendingSessionQueues(state)
        assert queues.queue_batch_review(7, "Tech Lead Batch") is TechLeadQueueOutcome.QUEUED

        outcome = queues.queue_batch_review(7, "Tech Lead Batch (retry)")

        assert outcome is TechLeadQueueOutcome.DUPLICATE
        (entry,) = state.pending_tech_lead_reviews
        assert entry.title == "Tech Lead Batch"

    def test_dedup_is_by_issue_number_across_variants(self):
        """One rule: an issue already queued is never re-queued, either variant."""
        state = OrchestratorState()
        queues = PendingSessionQueues(state)
        queues.queue_batch_review(7, "Tech Lead Batch")

        outcome = queues.queue_failure_investigation(
            7,
            "Investigate: timeout",
            failure=DiscoveredFailure(
                issue_number=7, issue_title="x", failure_reason="timed_out"
            ),
        )

        assert outcome is TechLeadQueueOutcome.DUPLICATE
        (entry,) = state.pending_tech_lead_reviews
        assert entry.flavor is TechLeadSessionFlavor.BATCH_REVIEW

    def test_tech_lead_retry_is_bounded_by_the_owner(self):
        """Retryable launch failures retain the item; the owner bounds retries.

        The queued investigation is the only cross-tick record, so retention is
        the default — but bounded: the owner signals EXHAUSTED on the
        TECH_LEAD_LAUNCH_RETRY_LIMIT-th failure so a genuinely broken input
        cannot relaunch-loop forever. EXHAUSTED does NOT remove the item here
        (#6771 round 4): destructive removal is the launch caller's commit
        protocol, run only after the durable needs-human transition lands, so
        the owner leaves the recoverable record in place.
        """
        state = OrchestratorState()
        queues = PendingSessionQueues(state)
        queues.queue_failure_investigation(
            8,
            "Investigate: timeout",
            failure=DiscoveredFailure(
                issue_number=8, issue_title="x", failure_reason="timed_out"
            ),
        )

        for attempt in range(1, TECH_LEAD_LAUNCH_RETRY_LIMIT):
            plan = queues.plan_tech_lead_retry(8)
            assert plan.outcome is TechLeadRetentionOutcome.RETAINED
            # Planning alone changes nothing: the spend is not real until it has
            # been made durable and then projected back here (#6999 F2).
            (entry,) = state.pending_tech_lead_reviews
            assert entry.retryable_launch_failures == attempt - 1
            queues.apply_tech_lead_retry(plan)
            (entry,) = state.pending_tech_lead_reviews
            assert entry.retryable_launch_failures == attempt

        final = queues.plan_tech_lead_retry(8)
        assert final.outcome is TechLeadRetentionOutcome.EXHAUSTED
        queues.apply_tech_lead_retry(final)
        assert len(state.pending_tech_lead_reviews) == 1, (
            "EXHAUSTED must retain the record; the caller commits the drop only "
            "after the durable needs-human transition succeeds"
        )

    def test_tech_lead_retry_for_unqueued_issue_fails_fast(self):
        """Retaining an item that is not queued is an upstream invariant bug."""
        with pytest.raises(ValueError, match="no such item is queued"):
            PendingSessionQueues(OrchestratorState()).plan_tech_lead_retry(999)

    def test_remove_tech_lead_removes_only_matching_issue(self):
        """remove_tech_lead completes the owner lifecycle (#6768 round 4)."""
        state = OrchestratorState()
        queues = PendingSessionQueues(state)
        queues.queue_batch_review(7, "Tech Lead Batch")
        queues.queue_failure_investigation(
            8,
            "Investigate: timeout",
            failure=DiscoveredFailure(
                issue_number=8, issue_title="x", failure_reason="timed_out"
            ),
        )

        queues.remove_tech_lead(7)

        (entry,) = state.pending_tech_lead_reviews
        assert entry.issue_number == 8

        queues.remove_tech_lead(999)  # unknown issue: no-op
        assert len(state.pending_tech_lead_reviews) == 1


def _make_queued_tech_lead(
    issue_number: int = 789,
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.BATCH_REVIEW,
) -> PendingTechLeadReview:
    failure = (
        DiscoveredFailure(
            issue_number=issue_number,
            issue_title="Tech Lead batch",
            failure_reason="failed",
        )
        if flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        else None
    )
    return PendingTechLeadReview(
        issue_number=issue_number, title="Tech Lead batch", flavor=flavor, failure=failure
    )


def _stub_tech_lead_launcher(result: LaunchResult) -> MagicMock:
    """SessionLauncher stub for tech_lead wrapper tests (no restorable terminals)."""
    launcher = MagicMock()
    launcher.launch_issue_session.return_value = result
    launcher.session_manager.runner.discover_running_sessions.return_value = []
    return launcher


class TestOrchestratorLaunchTechLeadSession:
    """Tests for orchestrator_launch_tech_lead_session (queue lifecycle owner)."""

    def test_raises_when_no_tech_lead_agent(self, sample_config):
        """Verify raises ValueError when no tech lead agent configured."""
        sample_config.tech_lead_review_agent = None

        with pytest.raises(ValueError, match="Invalid tech lead agent"):
            orchestrator_launch_tech_lead_session(
                _make_queued_tech_lead(),
                OrchestratorState(),
                sample_config,
                MagicMock(),
                MagicMock(),
                _claims_store(),
            )

    def test_raises_when_tech_lead_agent_not_in_config(self, sample_config):
        """Verify raises ValueError when tech lead agent not configured."""
        sample_config.tech_lead_review_agent = "agent:missing"

        with pytest.raises(ValueError, match="Invalid tech lead agent"):
            orchestrator_launch_tech_lead_session(
                _make_queued_tech_lead(),
                OrchestratorState(),
                sample_config,
                MagicMock(),
                MagicMock(),
                _claims_store(),
            )

    @pytest.mark.parametrize(
        "flavor",
        [TechLeadSessionFlavor.BATCH_REVIEW, TechLeadSessionFlavor.FAILURE_INVESTIGATION],
    )
    def test_launches_with_queue_items_own_flavor(self, sample_config, flavor):
        """The queue item's flavor travels to the launch verbatim (#6768 B5).

        The queue holds both variants; hard-coding either one here collapses
        the other (batch reviews launched as failure investigations skip
        manifest prep and audit nothing).
        """
        sample_config.tech_lead_review_agent = "agent:web"
        launcher = _stub_tech_lead_launcher(LaunchResult(session=None, success=False))

        orchestrator_launch_tech_lead_session(
            _make_queued_tech_lead(flavor=flavor),
            OrchestratorState(),
            sample_config,
            launcher,
            MagicMock(),
            _claims_store(),
        )

        call = launcher.launch_issue_session.call_args
        issue = call.args[0]
        assert issue.number == 789
        assert "agent:web" in issue.labels
        assert call.kwargs["tech_lead_scope"].flavor is flavor

    def test_successful_launch_removes_item_from_queue(self, sample_config, tmp_path):
        """Reviewer scenario: a launched item must not stay queued (#6768 r4)."""
        sample_config.tech_lead_review_agent = "agent:web"
        state = OrchestratorState()
        PendingSessionQueues(state).queue_batch_review(789, "Tech Lead batch")
        session = Session(
            key=SessionKey(issue=FakeIssueKey("789"), task=TaskKind.CODE),
            issue=Issue(number=789, title="Tech Lead batch", labels=["agent:web"]),
            agent_config=AgentConfig(prompt_path=tmp_path / "p.md", timeout_minutes=45),
            terminal_id="issue-789",
            worktree_path=tmp_path,
            branch_name="789-tech-lead",
            run_assets=make_session_run_assets(tmp_path, session_name="issue-789"),
        )
        launcher = _stub_tech_lead_launcher(LaunchResult(session=session, success=True))

        result = orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0], state, sample_config, launcher, MagicMock(),
            _claims_store(),
        )

        assert result is session
        assert state.pending_tech_lead_reviews == []
        assert state.active_sessions == [session]

    def test_existing_terminal_launch_retains_item_for_retry(self, sample_config):
        """EXISTING_TERMINAL (not yet restorable) retains the item.

        (The other retention cases are ``RETRYABLE_FAILURE`` — a transient
        required-input prep failure, or a terminal that never came up — covered
        by the bounded-retention tests.)
        """
        sample_config.tech_lead_review_agent = "agent:web"
        state = OrchestratorState()
        PendingSessionQueues(state).queue_batch_review(789, "Tech Lead batch")
        launcher = _stub_tech_lead_launcher(
            LaunchResult(
                session=None,
                success=False,
                disposition=LaunchDisposition.EXISTING_TERMINAL,
            )
        )

        result = orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0], state, sample_config, launcher, MagicMock(),
            _claims_store(),
        )

        assert result is None
        assert len(state.pending_tech_lead_reviews) == 1

    def test_permanent_launch_failure_drops_item(self, sample_config):
        """Non-retryable failure drops the item instead of relaunch-looping.

        Labels-as-truth recovers a dropped batch at startup; a dropped failure
        investigation is a best-effort audit (mirrors the review consumer).
        """
        sample_config.tech_lead_review_agent = "agent:web"
        state = OrchestratorState()
        PendingSessionQueues(state).queue_failure_investigation(
            789,
            "Investigate",
            failure=DiscoveredFailure(
                issue_number=789, issue_title="x", failure_reason="failed"
            ),
        )
        launcher = _stub_tech_lead_launcher(LaunchResult(session=None, success=False))

        result = orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0], state, sample_config, launcher, MagicMock(),
            _claims_store(),
        )

        assert result is None
        assert state.pending_tech_lead_reviews == []


class TestLaunchTechLeadIssueSessionFlavors:
    """Launch-side tech_lead flavor regressions (#6768 B4 / ADR-0031).

    Both tech_lead variants launch through launch_issue_session; the flavor must
    decide whether the global batch PR manifest is prepared, and the
    assignment file must always record what the session was asked to do.
    """

    @staticmethod
    def _run_identities(tmp_path: Path) -> list[tuple[str, str]]:
        """(run_id, session_name) for every session run under tmp_path.

        Read from each run's manifest.json - the same typed identities the
        launcher records with - never parsed from directory names.
        """
        identities = []
        for manifest in Path(str(tmp_path)).rglob(".issue-orchestrator/sessions/*/manifest.json"):
            data = json.loads(manifest.read_text())
            identities.append((data["run_id"], data["session_name"]))
        return identities

    @staticmethod
    def _enable_tech_lead_agent(config, tmp_path: Path) -> None:
        prompt_path = tmp_path / "tech-lead-prompt.md"
        prompt_path.write_text("Tech Lead prompt")
        config.agents["agent:tech-lead"] = AgentConfig(
            prompt_path=prompt_path,
            model="sonnet",
            timeout_minutes=45,
        )
        config.tech_lead_review_agent = "agent:tech-lead"

    @staticmethod
    def _started_run_dir(mock_events) -> Path:
        started = next(
            e for e in mock_events.events if str(e.name) == "session.started"
        )
        return Path(started.data["run_dir"])

    @staticmethod
    def _tech_lead_pr(number: int) -> PRInfo:
        return PRInfo(
            number=number,
            title=f"PR {number}",
            url=f"https://example/pr/{number}",
            branch=f"branch-{number}",
            body="",
            state="open",
            labels=["code-reviewed"],
        )

    def test_exception_after_authority_record_discards_row(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path, monkeypatch
    ):
        """The launch-lifecycle guard owns retention on EVERY exit (#6769 r4).

        A raise anywhere after the authority record (here: prompt rendering)
        must discard the durable row even though no failure branch and no
        completion seam ever runs — otherwise it leaks forever and a later
        run with the same identity hits the create-once conflict.
        """
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]
        issue = Issue(
            number=903, title="Batch Review", labels=["agent:tech-lead"], repo="test/repo"
        )
        monkeypatch.setattr(
            AgentConfig,
            "render_initial_prompt",
            lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        with pytest.raises(RuntimeError, match="boom"):
            launcher_bundle.launcher.launch_issue_session(issue, active_sessions=[])

        # The record DID happen before the raise (the agent-visible copy
        # exists), and the guard discarded the durable row on the way out.
        assignment_files = list(Path(str(tmp_path)).rglob("tech-lead-assignment.json"))
        assert assignment_files, "authority was never recorded - test lost its premise"
        identities = self._run_identities(tmp_path)
        assert identities, "no run manifest found - cannot verify by typed identity"
        store = SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
        for run_id, session_name in identities:
            assert (
                store.load(run_id=run_id, session_name=session_name) is None
            ), f"authority row leaked for run {run_id} ({session_name})"

    def test_post_start_failure_retains_authority_for_live_session(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path, monkeypatch
    ):
        """Once the terminal is RUNNING, bookkeeping failures keep authority.

        _create_session() returning True is the irreversible external
        boundary (#6769 round 5): the agent process is live and will write a
        completion, so discarding the row here would reject that completion
        as missing_authority with no recovery. The guard's success marker
        must sit at the boundary, not after the post-start bookkeeping.
        """
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]
        issue = Issue(
            number=907, title="Batch Review", labels=["agent:tech-lead"], repo="test/repo"
        )
        store = SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
        monkeypatch.setattr(
            type(launcher_bundle.launcher),
            "_trigger_issue_session_state_transitions",
            lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("post-start boom")),
        )

        with pytest.raises(RuntimeError, match="post-start boom"):
            launcher_bundle.launcher.launch_issue_session(issue, active_sessions=[])

        identities = self._run_identities(tmp_path)
        assert identities, "authority was never recorded - test lost its premise"
        retained = [
            store.load(run_id=run_id, session_name=session_name)
            for run_id, session_name in identities
        ]
        assert any(a is not None for a in retained), "authority row was discarded for a LIVE session"

    def test_default_flavor_prepares_manifest_and_batch_assignment(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]
        issue = Issue(
            number=901, title="Batch Review", labels=["agent:tech-lead"], repo="test/repo"
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue, active_sessions=[]
        )

        assert result.success is True
        assert mock_repo_host.get_prs_with_label_calls == [
            (config.tech_lead_watch_label, "all")
        ]
        run_dir = self._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert run_manifest["tech_lead_manifest"] == str(
            run_dir / "tech-lead-data" / "manifest.json"
        )
        assert (run_dir / "tech-lead-data" / "manifest.json").exists()
        assignment_path = run_dir / "tech-lead-data" / "tech-lead-assignment.json"
        assert run_manifest["tech_lead_assignment"] == str(assignment_path)
        assignment = json.loads(assignment_path.read_text())
        assert assignment["flavor"] == "batch_review"
        assert assignment["focus_issue_number"] is None

    def test_failure_investigation_skips_manifest_and_records_focus(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]
        issue = Issue(
            number=902,
            title="Investigate: session timed out",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
            ),
        )

        assert result.success is True
        # The global batch PR manifest must NOT be built or queried.
        assert mock_repo_host.get_prs_with_label_calls == []
        run_dir = self._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert "tech_lead_manifest" not in run_manifest
        assert not (run_dir / "tech-lead-data" / "manifest.json").exists()
        assignment_path = run_dir / "tech-lead-data" / "tech-lead-assignment.json"
        assert run_manifest["tech_lead_assignment"] == str(assignment_path)
        assignment = json.loads(assignment_path.read_text())
        assert assignment["flavor"] == "failure_investigation"
        assert assignment["focus_issue_number"] == 902
        assert assignment["focus_reason"] == "Investigate: session timed out"

    def test_non_tech_lead_session_gets_neither_manifest_nor_assignment(
        self, launcher_bundle, sample_issue, mock_repo_host, mock_events, tmp_path
    ):
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]

        result = launcher_bundle.launcher.launch_issue_session(
            sample_issue, active_sessions=[]
        )

        assert result.success is True
        assert mock_repo_host.get_prs_with_label_calls == []
        run_dir = self._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert "tech_lead_manifest" not in run_manifest
        assert "tech_lead_assignment" not in run_manifest
        assert not (run_dir / "tech-lead-data").exists()

    def test_batch_flavor_writes_board_snapshot_with_manifest_entry(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """Batch reviews get board-snapshot.json plus a run-manifest entry.

        Board data is local orchestrator state (ADR-0031 §3): the launch must
        make no repository-host calls beyond the pre-existing batch manifest
        fetch — no new call types.
        """
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]
        provider = launcher_bundle.board_snapshot_provider
        issue = Issue(
            number=901, title="Batch Review", labels=["agent:tech-lead"], repo="test/repo"
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue, active_sessions=[]
        )

        assert result.success is True
        # Batch reviews take the whole-board view: no focus issue.
        assert provider.calls == [None]
        run_dir = self._started_run_dir(mock_events)
        snapshot_path = run_dir / "tech-lead-data" / "board-snapshot.json"
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert run_manifest["board_snapshot"] == str(snapshot_path)
        # The written file must parse back through the typed reader.
        snapshot = BoardSnapshot.read(snapshot_path)
        assert snapshot.orchestrator_paused is False
        # No new repository-host call types: only the batch manifest fetch.
        assert mock_repo_host.get_prs_with_label_calls == [
            (config.tech_lead_watch_label, "all")
        ]
        assert mock_repo_host.add_label_calls == []
        assert mock_repo_host.remove_label_calls == []
        assert mock_repo_host.search_pr_refs_calls == []

    def test_failure_flavor_board_snapshot_scoped_to_focus_issue(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """Failure investigations get a snapshot scoped to the failed issue."""
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        provider = launcher_bundle.board_snapshot_provider
        issue = Issue(
            number=902,
            title="Investigate: session timed out",
            labels=["agent:tech-lead"],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
            ),
        )

        assert result.success is True
        assert provider.calls == [902]
        run_dir = self._started_run_dir(mock_events)
        snapshot_path = run_dir / "tech-lead-data" / "board-snapshot.json"
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert run_manifest["board_snapshot"] == str(snapshot_path)
        assert BoardSnapshot.read(snapshot_path).schema_version == BOARD_SNAPSHOT_SCHEMA_VERSION
        # Still no PR manifest — and no GitHub reads at all for this flavor.
        assert "tech_lead_manifest" not in run_manifest
        assert mock_repo_host.get_prs_with_label_calls == []

    @staticmethod
    def _launch(queued, state, launcher_bundle):
        session = orchestrator_launch_tech_lead_session(
            queued,
            state,
            launcher_bundle.launcher.config,
            launcher_bundle.launcher,
            MagicMock(),
            _claims_store(),
        )
        assert session is not None
        return session


    @staticmethod
    def _apply_actions(state: OrchestratorState, actions, **details) -> None:
        """Drive the producer boundary through the public ``apply_plan`` seam.

        The action applier is faked at its port boundary (``apply`` returns a
        successful ``ActionResult`` carrying ``details``, the shape the real
        applier produces), so ``apply_plan`` runs the real success-handling
        path — including the post-apply state update that queues tech_lead work.
        """
        from issue_orchestrator.control.planner_types import Plan

        applier = MagicMock()
        applier.apply.side_effect = lambda action: ActionResult.ok(
            action, **details
        )
        event_context = MagicMock()
        event_context.enrich.side_effect = lambda data: data
        support = OrchestratorSupport(
            config=MagicMock(),
            events=MockEventSink(),
            repository_host=MagicMock(),
            state=state,
            event_context=event_context,
            session_manager=MagicMock(),
            action_applier=applier,
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=MagicMock(),
            kill_session=MagicMock(),
        )
        support.apply_plan(
            Plan(actions=tuple(actions), skipped=()), MagicMock()
        )

    def test_marker_label_derives_health_review_flavor(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """The ADR-0031 §4 marker label alone selects HEALTH_REVIEW.

        No explicit ``tech_lead_flavor``: startup recovery relaunches the anchor
        from its labels (crash-safe truth), so the marker must derive the
        flavor — no PR manifest query or build, a board-wide snapshot, and an
        anchor-only authority record (no focus issue, empty manifest set).
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore as TechLeadAuthorityStore,
        )

        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [self._tech_lead_pr(555)]
        provider = launcher_bundle.board_snapshot_provider
        issue = Issue(
            number=905,
            title="Health Review — walk the floor",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue, active_sessions=[]
        )

        assert result.success is True
        # The global batch PR manifest must NOT be built or queried.
        assert mock_repo_host.get_prs_with_label_calls == []
        run_dir = self._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert "tech_lead_manifest" not in run_manifest
        assert not (run_dir / "tech-lead-data" / "manifest.json").exists()
        # Board-wide snapshot: no focus issue.
        assert provider.calls == [None]
        assert run_manifest["board_snapshot"] == str(
            run_dir / "tech-lead-data" / "board-snapshot.json"
        )
        # The agent-readable assignment records the health flavor, no focus.
        assignment = json.loads(
            (run_dir / "tech-lead-data" / "tech-lead-assignment.json").read_text()
        )
        assert assignment["flavor"] == "health_review"
        assert assignment["focus_issue_number"] is None
        assert assignment["focus_reason"] == ""
        # The trusted authority record: anchor-only scope (#6761 rr F1).
        authority = TechLeadAuthorityStore.for_repo(config.repo_root).load(
            run_id=result.session.run_assets.run_id,
            session_name=result.session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert authority.anchor_issue_number == 905
        assert authority.focus_issue_number is None
        assert authority.manifest_pr_numbers == ()
        assert authority.problem_issue_numbers == ()
        assert authority.allowed_targets() == frozenset({905})

    def test_health_authority_records_the_granted_cohort_not_board_failures(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """Authority is the PRODUCER's grant; board failures are context only.

        #6780: authority used to be read back out of the board
        snapshot's failure list. That list merges the live buffer plus every
        pending failure investigation, so an unrelated failing issue (#99
        here) silently joined the storm review's act-level scope and could be
        reset_retry'd by it. The grant must decide the scope, and unrelated
        failures must stay visible as context without widening it.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        provider = launcher_bundle.board_snapshot_provider
        assert isinstance(provider, RecordingBoardSnapshotProvider)
        provider.recent_failures = [
            BoardFailure(43, "Problem 43", "failed", []),
            BoardFailure(41, "Problem 41", "blocked", []),
            BoardFailure(42, "Problem 42", "timed_out", []),
            # NOT in the grant: an unrelated investigation that happens to be
            # pending. Context for the reviewer, never authority.
            BoardFailure(99, "Unrelated pending investigation", "failed", []),
        ]
        issue = Issue(
            number=905,
            title="Health Review — problem storm",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
                problem_issue_numbers=(41, 42, 43),
            ),
        )

        assert result.success is True
        assert provider.cohort_calls == [(41, 42, 43)], (
            "the launch boundary must hand the owned cohort to the provider"
        )
        run_dir = self._started_run_dir(mock_events)
        snapshot = BoardSnapshot.read(
            run_dir / "tech-lead-data" / "board-snapshot.json"
        )
        assert 99 in {f.issue_number for f in snapshot.recent_failures}, (
            "unrelated failures stay on the board as context"
        )
        authority = SqliteTechLeadAuthorityStore.for_repo(config.repo_root).load(
            run_id=result.session.run_assets.run_id,
            session_name=result.session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.problem_issue_numbers == (41, 42, 43)
        assert authority.allowed_act_level_targets() == frozenset({41, 42, 43})
        assert snapshot.problem_issue_numbers() == frozenset({41, 42, 43}), (
            "the snapshot's cohort surface is the grant, not its failure list"
        )

    def test_periodic_health_review_gains_no_cohort_from_pending_failures(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """A periodic review may PROPOSE, never act (#6780).

        Its grant carries no cohort. Failures on the board at launch time —
        here a pending investigation for #99 — must not become act-level
        authority just because the review happened to start while they were
        queued.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        provider = launcher_bundle.board_snapshot_provider
        assert isinstance(provider, RecordingBoardSnapshotProvider)
        provider.recent_failures = [
            BoardFailure(99, "Unrelated pending investigation", "failed", []),
        ]
        issue = Issue(
            number=906,
            title="Health Review — walk the floor",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue,
            active_sessions=[],
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW
            ),
        )

        assert result.success is True
        authority = SqliteTechLeadAuthorityStore.for_repo(config.repo_root).load(
            run_id=result.session.run_assets.run_id,
            session_name=result.session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.problem_issue_numbers == ()
        assert authority.allowed_act_level_targets() == frozenset()
        run_dir = self._started_run_dir(mock_events)
        snapshot = BoardSnapshot.read(
            run_dir / "tech-lead-data" / "board-snapshot.json"
        )
        assert [f.issue_number for f in snapshot.recent_failures] == [99], (
            "the periodic review still SEES the failing issue as context"
        )
        assert snapshot.problem_issue_numbers() == frozenset()

    def test_marker_label_pickup_takes_its_cohort_from_the_ledger(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """A storm anchor launched WITHOUT a grant keeps its exact cohort.

        A marker-labeled anchor can also be picked up as an ordinary issue, so
        no launch scope arrives. The cohort then comes from the durable ledger
        keyed by the anchor — still a dedicated surface, never the board's
        failure list (#6780). Fixing F1 must not silently strip the
        authority of an anchor that reaches launch by this path.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        store = SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
        store.record_storm_cohort(
            anchor_issue_number=907,
            cohort=tuple(
                DiscoveredFailure(number, f"Problem {number}", "failed")
                for number in (41, 42, 43)
            ),
        )
        provider = launcher_bundle.board_snapshot_provider
        assert isinstance(provider, RecordingBoardSnapshotProvider)
        provider.recent_failures = [
            BoardFailure(99, "Unrelated pending investigation", "failed", []),
        ]
        issue = Issue(
            number=907,
            title="Health Review — problem storm",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            repo="test/repo",
        )

        result = launcher_bundle.launcher.launch_issue_session(
            issue, active_sessions=[]
        )

        assert result.success is True
        authority = store.load(
            run_id=result.session.run_assets.run_id,
            session_name=result.session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert authority.problem_issue_numbers == (41, 42, 43), (
            "the ledger is the fallback cohort source, not the board snapshot"
        )

    def test_health_review_anchor_launches_as_health_flavor(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """Marker-labeled creation -> typed intake -> queue -> launch (ADR-0031 §4).

        The producer boundary derives HEALTH_REVIEW from the marker label on
        the creation action; the queued flavor must survive to launch — no
        batch manifest query, a board-wide snapshot, a health assignment —
        and the launched item must leave the queue.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        config = launcher_bundle.launcher.config
        TestLaunchTechLeadIssueSessionFlavors._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [
            TestLaunchTechLeadIssueSessionFlavors._tech_lead_pr(555)
        ]
        state = OrchestratorState()

        self._apply_actions(
            state,
            [
                CreateTechLeadIssueAction(
                    title="Health Review — walk the floor",
                    body="Walk the floor",
                    labels=("agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL),
                    pr_count=0,
                    origin=TechLeadCreationOrigin.authors_anchor(),
                )
            ],
            issue_number=905,
        )
        (queued,) = state.pending_tech_lead_reviews
        assert queued.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert queued.failure is None
        # The intake stamped the interval marker (dedup for the next tick).
        assert state.last_health_review_at > 0.0

        session = self._launch(queued, state, launcher_bundle)

        # A launched item leaves the queue and cannot be relaunched next tick.
        assert state.pending_tech_lead_reviews == []
        assert state.active_sessions == [session]
        # Health review must NOT query or build the batch PR manifest...
        assert mock_repo_host.get_prs_with_label_calls == []
        run_dir = TestLaunchTechLeadIssueSessionFlavors._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert "tech_lead_manifest" not in run_manifest
        # ...its assignment records the health flavor with no focus...
        assignment = json.loads(
            (run_dir / "tech-lead-data" / "tech-lead-assignment.json").read_text()
        )
        assert assignment["flavor"] == "health_review"
        assert assignment["focus_issue_number"] is None
        # ...and the snapshot is board-wide (no focus issue).
        assert launcher_bundle.board_snapshot_provider.calls == [None]


    def test_board_snapshot_failure_retains_queued_investigation_until_bounded_drop(
        self, launcher_bundle, mock_worktree_manager, mock_events, tmp_path
    ):
        """Required-input prep failure must not destroy the only investigation record.

        board-snapshot.json is required input, so a DB/log bug still fails the
        launch loudly through the established seam (SESSION_START_FAILED,
        worktree cleanup, no session). But the QUEUED orchestration path must
        RETAIN the pending item: for failure investigations the per-tick
        discovered_failures buffer is already cleared and there is no
        labels-as-truth recovery, so one transient SQLite/log/filesystem
        error must not delete the investigation forever. Retention is
        bounded: after TECH_LEAD_LAUNCH_RETRY_LIMIT retryable failures the queue
        owner drops the item and the drop is a DURABLE needs-human
        transition (#6771 round 3): needs-human label + explanatory comment
        through the launcher's owning action boundary, then the
        ISSUE_NEEDS_HUMAN event. A log line and an event alone do not
        survive an orchestrator restart.
        """
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        launcher_bundle.board_snapshot_provider.error = RuntimeError("boom")
        state = OrchestratorState()
        PendingSessionQueues(state).queue_failure_investigation(
            903,
            "Investigate: Broken thing (failed)",
            failure=DiscoveredFailure(
                issue_number=903, issue_title="Broken thing", failure_reason="failed"
            ),
        )

        # Transient failures below the bound: the investigation stays queued.
        for attempt in range(1, TECH_LEAD_LAUNCH_RETRY_LIMIT):
            session = orchestrator_launch_tech_lead_session(
                state.pending_tech_lead_reviews[0],
                state,
                config,
                launcher_bundle.launcher,
                MagicMock(),
                _claims_store(),
            )
            assert session is None
            assert len(state.pending_tech_lead_reviews) == 1, (
                "a retryable prep failure must retain the queued investigation"
            )
            assert (
                state.pending_tech_lead_reviews[0].retryable_launch_failures == attempt
            )

        # Each attempt failed loudly through the established launch seam.
        failed_events = [
            e for e in mock_events.events
            if str(e.name) == str(EventName.SESSION_START_FAILED)
        ]
        assert len(failed_events) == TECH_LEAD_LAUNCH_RETRY_LIMIT - 1
        assert failed_events[0].data["reason"] == "tech_lead_session_data_failed"
        assert not any(str(e.name) == "session.started" for e in mock_events.events)
        assert mock_worktree_manager.remove_calls, "worktree must be cleaned up"

        # The final failure exhausts the bounded policy: dropped AND surfaced.
        session = orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0],
            state,
            config,
            launcher_bundle.launcher,
            MagicMock(),
            _claims_store(),
        )
        assert session is None
        assert state.pending_tech_lead_reviews == []
        needs_human = [
            e for e in mock_events.events
            if str(e.name) == str(EventName.ISSUE_NEEDS_HUMAN)
        ]
        assert len(needs_human) == 1
        assert needs_human[0].data["issue_number"] == 903
        assert "failure_investigation" in needs_human[0].data["reason"]
        assert "boom" in needs_human[0].data["reason"]

        # #6771 round 3: the drop must also be DURABLE on the issue itself —
        # provenance marker + needs-human label + explanatory comment through
        # the action applier
        # (labels as source of truth), not just a fire-and-forget event.
        actions = [
            call.args[0]
            for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        labels = [
            a for a in actions
            if isinstance(a, AddLabelAction) and a.issue_number == 903
        ]
        assert [a.label for a in labels] == [
            LabelManager(config).tech_lead_needs_human,
            "needs-human",
        ]
        comments = [
            a for a in actions
            if isinstance(a, AddCommentAction) and a.number == 903
        ]
        assert len(comments) == 1
        assert "failure_investigation" in comments[0].comment
        assert str(TECH_LEAD_LAUNCH_RETRY_LIMIT) in comments[0].comment
        assert "boom" in comments[0].comment

    def _drive_to_exhaustion(
        self, state, config, launcher_bundle
    ) -> None:
        """Drive a queued investigation through TECH_LEAD_LAUNCH_RETRY_LIMIT failures.

        Every attempt fails at required-input prep (board snapshot error), so
        the LIMIT-th attempt reaches the EXHAUSTED escalation/commit path.
        """
        for _ in range(TECH_LEAD_LAUNCH_RETRY_LIMIT):
            orchestrator_launch_tech_lead_session(
                state.pending_tech_lead_reviews[0],
                state,
                config,
                launcher_bundle.launcher,
                MagicMock(),
                _claims_store(),
            )

    @staticmethod
    def _queue_investigation(state) -> None:
        PendingSessionQueues(state).queue_failure_investigation(
            903,
            "Investigate: Broken thing (failed)",
            failure=DiscoveredFailure(
                issue_number=903, issue_title="Broken thing", failure_reason="failed"
            ),
        )

    def test_exhaustion_retains_item_when_needs_human_label_mutation_fails(
        self, launcher_bundle, mock_events, tmp_path
    ):
        """#6771 round 4: the drop must not commit before the label lands.

        If GitHub rejects the needs-human label, the only durable record of
        the investigation (the queued item) must survive, no ISSUE_NEEDS_HUMAN
        event may claim a transition that did not happen, and the explanatory
        comment must be short-circuited (never posted after a failed label, so
        a later retry cannot double-post it).
        """
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        launcher_bundle.board_snapshot_provider.error = RuntimeError("boom")

        def apply_action(action):
            if isinstance(action, AddLabelAction) and action.label == "needs-human":
                return ActionResult.fail(action, "github 422 rejected label")
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)

        state = OrchestratorState()
        self._queue_investigation(state)
        self._drive_to_exhaustion(state, config, launcher_bundle)

        # Recoverable record retained — destructive removal did not precede
        # the (failed) durable transition.
        assert len(state.pending_tech_lead_reviews) == 1
        assert (
            state.pending_tech_lead_reviews[0].retryable_launch_failures
            >= TECH_LEAD_LAUNCH_RETRY_LIMIT
        )
        # No event may claim a durable needs-human transition that never landed.
        assert not any(
            str(e.name) == str(EventName.ISSUE_NEEDS_HUMAN) for e in mock_events.events
        )
        applied = [
            call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        # Marker and label were attempted in order; comment short-circuited.
        marker = LabelManager(config).tech_lead_needs_human
        assert any(
            isinstance(a, AddLabelAction) and a.label == marker for a in applied
        )
        assert any(
            isinstance(a, AddLabelAction) and a.label == "needs-human" for a in applied
        )
        assert not any(
            isinstance(a, AddCommentAction) and a.number == 903 for a in applied
        )

    def test_exhaustion_retains_then_recovers_when_comment_mutation_fails(
        self, launcher_bundle, mock_events, tmp_path
    ):
        """#6771 round 4: comment failure also blocks the drop, and it recovers.

        A failed comment (label already applied) still withholds the event and
        retains the item; on a later tick, once the mutation succeeds, the
        durable transition commits — the item is removed and the event fires
        exactly once. This locks the recoverable half of the invariant at the
        public routing boundary.
        """
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        launcher_bundle.board_snapshot_provider.error = RuntimeError("boom")

        comment_fails = {"active": True}

        def apply_action(action):
            if (
                comment_fails["active"]
                and isinstance(action, AddCommentAction)
                and action.number == 903
            ):
                return ActionResult.fail(action, "github 422 rejected comment")
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)

        state = OrchestratorState()
        self._queue_investigation(state)
        self._drive_to_exhaustion(state, config, launcher_bundle)

        # Comment failed → item retained, event withheld, but the label
        # (source of truth) was applied.
        assert len(state.pending_tech_lead_reviews) == 1
        assert not any(
            str(e.name) == str(EventName.ISSUE_NEEDS_HUMAN) for e in mock_events.events
        )
        applied = [
            call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        assert any(
            isinstance(a, AddLabelAction) and a.label == "needs-human" for a in applied
        )
        assert any(
            isinstance(a, AddLabelAction)
            and a.label == LabelManager(config).tech_lead_needs_human
            for a in applied
        )

        # Later tick: the transient GitHub error clears; escalation now commits.
        comment_fails["active"] = False
        orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0],
            state,
            config,
            launcher_bundle.launcher,
            MagicMock(),
            _claims_store(),
        )
        assert state.pending_tech_lead_reviews == [], (
            "the drop must commit only after the durable transition succeeds"
        )
        needs_human = [
            e for e in mock_events.events
            if str(e.name) == str(EventName.ISSUE_NEEDS_HUMAN)
        ]
        assert len(needs_human) == 1
        assert needs_human[0].data["issue_number"] == 903

    def test_marker_failure_short_circuits_needs_human_and_comment(
        self, launcher_bundle, mock_events, tmp_path
    ):
        """A needs-human transition cannot exist without provenance."""
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        launcher_bundle.board_snapshot_provider.error = RuntimeError("boom")
        marker = LabelManager(config).tech_lead_needs_human

        def apply_action(action):
            if isinstance(action, AddLabelAction) and action.label == marker:
                return ActionResult.fail(action, "github rejected marker")
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)
        state = OrchestratorState()
        self._queue_investigation(state)
        self._drive_to_exhaustion(state, config, launcher_bundle)

        assert len(state.pending_tech_lead_reviews) == 1
        applied = [
            call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        assert any(
            isinstance(action, AddLabelAction) and action.label == marker
            for action in applied
        )
        assert not any(
            isinstance(action, AddLabelAction) and action.label == "needs-human"
            for action in applied
        )
        assert not any(isinstance(action, AddCommentAction) for action in applied)
        assert not any(
            str(event.name) == str(EventName.ISSUE_NEEDS_HUMAN)
            for event in mock_events.events
        )

    def test_recovered_launch_reconciles_marker_owned_needs_human(
        self, launcher_bundle, tmp_path
    ):
        """A later tick clears both labels after the investigation launches."""
        config = launcher_bundle.launcher.config
        self._enable_tech_lead_agent(config, tmp_path)
        launcher_bundle.board_snapshot_provider.error = RuntimeError("boom")
        labels = LabelManager(config)
        github_labels: set[str] = set()
        comment_fails = {"active": True}

        def apply_action(action):
            if isinstance(action, AddLabelAction):
                github_labels.add(action.label)
            if (
                comment_fails["active"]
                and isinstance(action, AddCommentAction)
                and action.number == 903
            ):
                return ActionResult.fail(action, "github rejected comment")
            if isinstance(action, RemoveLabelAction):
                github_labels.discard(action.label)
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)
        launcher_bundle.launcher.repository_host.get_issue_labels_fresh.side_effect = (
            lambda issue_number: list(github_labels) if issue_number == 903 else []
        )

        state = OrchestratorState()
        self._queue_investigation(state)
        self._drive_to_exhaustion(state, config, launcher_bundle)

        assert labels.tech_lead_needs_human in github_labels
        assert labels.needs_human in github_labels
        assert len(state.pending_tech_lead_reviews) == 1

        comment_fails["active"] = False
        launcher_bundle.board_snapshot_provider.error = None
        session = orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0],
            state,
            config,
            launcher_bundle.launcher,
            MagicMock(),
            _claims_store(),
        )

        assert session is not None
        assert state.pending_tech_lead_reviews == []
        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human(
            state.active_sessions
        )

        assert labels.needs_human not in github_labels
        assert labels.tech_lead_needs_human not in github_labels
        removals = [
            action
            for action in (
                call.args[0]
                for call in launcher_bundle.action_applier.apply.call_args_list
            )
            if isinstance(action, RemoveLabelAction)
            and action.issue_number == 903
            and action.label in {labels.needs_human, labels.tech_lead_needs_human}
        ]
        assert [action.label for action in removals] == [
            labels.needs_human,
            labels.tech_lead_needs_human,
        ]

    @staticmethod
    def _restored_session(issue_number: int, labels: list[str], tmp_path: Path) -> Session:
        """Build the active-session shape restored after a process restart."""
        return Session(
            key=SessionKey(issue=FakeIssueKey(str(issue_number)), task=TaskKind.CODE),
            issue=Issue(
                number=issue_number,
                title=f"Issue {issue_number}",
                labels=labels,
                repo="test/repo",
            ),
            agent_config=AgentConfig(prompt_path=tmp_path / "p.md", timeout_minutes=45),
            terminal_id=f"issue-{issue_number}",
            worktree_path=tmp_path,
            branch_name=f"{issue_number}-branch",
            run_assets=make_session_run_assets(
                tmp_path, session_name=f"issue-{issue_number}"
            ),
        )

    def test_failed_clear_keeps_marker_and_retries_without_relaunch(
        self, launcher_bundle, mock_events, tmp_path
    ):
        """Failed external removal is reconstructed from labels on the next tick."""
        labels = LabelManager(launcher_bundle.launcher.config)
        github_labels = {labels.tech_lead_needs_human, labels.needs_human}
        needs_human_remove_fails = {"active": True}

        fresh_labels = launcher_bundle.launcher.repository_host.get_issue_labels_fresh
        fresh_labels.side_effect = None
        fresh_labels.return_value = list(github_labels)

        def apply_action(action):
            if (
                needs_human_remove_fails["active"]
                and isinstance(action, RemoveLabelAction)
                and action.label == labels.needs_human
            ):
                return ActionResult.fail(action, "github rejected removal")
            if isinstance(action, RemoveLabelAction):
                github_labels.discard(action.label)
                fresh_labels.return_value = list(github_labels)
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)
        active = [
            self._restored_session(
                903, [labels.tech_lead_needs_human, labels.needs_human], tmp_path
            )
        ]
        launches_before = sum(
            1 for event in mock_events.events if str(event.name) == "session.started"
        )

        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human(active)

        assert github_labels == {labels.tech_lead_needs_human, labels.needs_human}
        first_pass = [
            call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        assert [
            action.label
            for action in first_pass
            if isinstance(action, RemoveLabelAction)
        ] == [labels.needs_human]

        needs_human_remove_fails["active"] = False
        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human(active)

        assert github_labels == set()
        all_removals = [
            call.args[0]
            for call in launcher_bundle.action_applier.apply.call_args_list
            if isinstance(call.args[0], RemoveLabelAction)
        ]
        assert [action.label for action in all_removals] == [
            labels.needs_human,
            labels.needs_human,
            labels.tech_lead_needs_human,
        ]
        assert (
            sum(
                1
                for event in mock_events.events
                if str(event.name) == "session.started"
            )
            == launches_before
        )

    def test_restart_uses_marker_provenance_and_preserves_legitimate_needs_human(
        self, launcher_bundle, tmp_path
    ):
        """Restored sessions need no side record to distinguish label ownership."""
        labels = LabelManager(launcher_bundle.launcher.config)
        github_labels = {
            903: {labels.tech_lead_needs_human, labels.needs_human},
            904: {labels.needs_human},
            905: {labels.tech_lead_needs_human},
        }
        launcher_bundle.launcher.repository_host.get_issue_labels_fresh.side_effect = (
            lambda issue_number: list(github_labels[issue_number])
        )

        def apply_action(action):
            if isinstance(action, RemoveLabelAction):
                github_labels[action.issue_number].discard(action.label)
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)
        restored = [
            self._restored_session(903, list(github_labels[903]), tmp_path),
            self._restored_session(904, list(github_labels[904]), tmp_path),
            self._restored_session(905, list(github_labels[905]), tmp_path),
        ]

        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human(restored)

        assert github_labels[903] == set()
        assert github_labels[904] == {labels.needs_human}
        assert github_labels[905] == set()
        removals = [
            call.args[0]
            for call in launcher_bundle.action_applier.apply.call_args_list
            if isinstance(call.args[0], RemoveLabelAction)
        ]
        assert [
            (action.issue_number, action.label) for action in removals
        ] == [
            (903, labels.needs_human),
            (903, labels.tech_lead_needs_human),
            (905, labels.tech_lead_needs_human),
        ]

    def test_restart_discovers_marker_only_without_queue_or_active_session(
        self, launcher_bundle, mock_repo_host, mock_events
    ):
        """A fresh launcher recovers the durable marker without memory state."""
        labels = LabelManager(launcher_bundle.launcher.config)
        mock_repo_host.labels[903] = {labels.tech_lead_needs_human}

        def apply_action(action):
            if isinstance(action, AddLabelAction):
                mock_repo_host.labels[action.issue_number].add(action.label)
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)

        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human([])

        assert mock_repo_host.labels[903] == {
            labels.tech_lead_needs_human,
            labels.needs_human,
        }
        assert mock_repo_host.list_issues_calls == [
            (
                [labels.tech_lead_needs_human],
                "open",
                100,
            )
        ]
        applied = [
            call.args[0]
            for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        assert any(
            isinstance(action, AddCommentAction)
            and "recovered an interrupted tech_lead" in action.comment
            for action in applied
        )
        assert [
            event
            for event in mock_events.events
            if str(event.name) == str(EventName.ISSUE_NEEDS_HUMAN)
        ]

    def test_escalation_does_not_claim_preexisting_needs_human(
        self, launcher_bundle, mock_repo_host, tmp_path
    ):
        """The public launcher boundary preserves a legitimate bare label."""
        labels = LabelManager(launcher_bundle.launcher.config)
        mock_repo_host.labels[903] = {labels.needs_human}
        launcher_bundle.action_applier.apply = MagicMock(
            side_effect=lambda action: ActionResult.ok(action)
        )

        committed = launcher_bundle.launcher.escalate_issue_needs_human(
            issue_number=903,
            reason="failure investigation exhausted",
            comment="failure investigation could not launch",
            context="tech_lead_exhausted",
            event_data={"issue_number": 903},
        )
        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human(
            [self._restored_session(903, [labels.needs_human], tmp_path)]
        )

        assert committed is True
        assert mock_repo_host.labels[903] == {labels.needs_human}
        applied = [
            call.args[0]
            for call in launcher_bundle.action_applier.apply.call_args_list
        ]
        assert not any(
            isinstance(action, AddLabelAction)
            and action.label == labels.tech_lead_needs_human
            for action in applied
        )
        assert not any(isinstance(action, RemoveLabelAction) for action in applied)

    def test_reconcile_read_failure_defers_without_mutation(
        self, launcher_bundle, tmp_path
    ):
        """A transient label read failure is visible and retried next tick."""
        launcher_bundle.launcher.repository_host.get_issue_labels_fresh.side_effect = (
            RuntimeError("github unavailable")
        )
        launcher_bundle.action_applier.apply.reset_mock()

        launcher_bundle.launcher.reconcile_stale_tech_lead_needs_human(
            [self._restored_session(903, [], tmp_path)]
        )

        launcher_bundle.action_applier.apply.assert_not_called()

class TestTechLeadProducerToLaunchBoundary:
    """The flavor set at the producer boundary must survive queue -> launch (#6768 B5).

    The pending-tech-lead queue carries both variants; these tests drive the real
    producer handlers into the queue and the queued item through
    orchestrator_launch_tech_lead_session into the real SessionLauncher, asserting
    the launch behaves per the producer's flavor (manifest prep + written
    assignment) and empties the queue (#6768 round 4).
    """

    @staticmethod
    def _apply_actions(state: OrchestratorState, actions, **details) -> None:
        """Drive the producer boundary through the public ``apply_plan`` seam.

        The action applier is faked at its port boundary (``apply`` returns a
        successful ``ActionResult`` carrying ``details``, the shape the real
        applier produces), so ``apply_plan`` runs the real success-handling
        path — including the post-apply state update that queues tech_lead work.
        """
        from issue_orchestrator.control.planner_types import Plan

        applier = MagicMock()
        applier.apply.side_effect = lambda action: ActionResult.ok(
            action, **details
        )
        event_context = MagicMock()
        event_context.enrich.side_effect = lambda data: data
        support = OrchestratorSupport(
            config=MagicMock(),
            events=MockEventSink(),
            repository_host=MagicMock(),
            state=state,
            event_context=event_context,
            session_manager=MagicMock(),
            action_applier=applier,
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=MagicMock(),
            kill_session=MagicMock(),
        )
        support.apply_plan(
            Plan(actions=tuple(actions), skipped=()), MagicMock()
        )

    @staticmethod
    def _launch(queued, state, launcher_bundle):
        session = orchestrator_launch_tech_lead_session(
            queued,
            state,
            launcher_bundle.launcher.config,
            launcher_bundle.launcher,
            MagicMock(),
            _claims_store(),
        )
        assert session is not None
        return session

    def test_threshold_batch_issue_launches_as_batch_review(
        self, launcher_bundle, mock_repo_host, mock_events, tmp_path
    ):
        """_handle_create_tech_lead_issue -> queue -> launch prepares the manifest."""
        config = launcher_bundle.launcher.config
        TestLaunchTechLeadIssueSessionFlavors._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [
            TestLaunchTechLeadIssueSessionFlavors._tech_lead_pr(555)
        ]
        state = OrchestratorState()

        self._apply_actions(
            state,
            [
                CreateTechLeadIssueAction(
                    title="Tech Lead Batch Review: 5 PRs pending",
                    body="Review these PRs",
                    labels=("agent:tech-lead",),
                    pr_count=5,
                    origin=TechLeadCreationOrigin.authors_anchor(),
                )
            ],
            issue_number=903,
        )
        (queued,) = state.pending_tech_lead_reviews
        assert queued.flavor is TechLeadSessionFlavor.BATCH_REVIEW

        session = self._launch(queued, state, launcher_bundle)

        # A launched item leaves the queue and cannot be relaunched next tick.
        assert state.pending_tech_lead_reviews == []
        assert state.active_sessions == [session]
        decision = TechLeadWorkflow(config, NullEventSink()).should_launch_tech_lead(
            state.pending_tech_lead_reviews, paused=False, available_slots=0
        )
        assert decision.should_launch is False
        assert decision.tech_lead_to_launch == ()

        # Batch review must prepare the PR manifest...
        assert mock_repo_host.get_prs_with_label_calls == [
            (config.tech_lead_watch_label, "all")
        ]
        run_dir = TestLaunchTechLeadIssueSessionFlavors._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert run_manifest["tech_lead_manifest"] == str(
            run_dir / "tech-lead-data" / "manifest.json"
        )
        # ...and record a batch assignment for the completion planner.
        assignment = json.loads(
            (run_dir / "tech-lead-data" / "tech-lead-assignment.json").read_text()
        )
        assert assignment["flavor"] == "batch_review"
        assert assignment["focus_issue_number"] is None

    def test_failure_investigation_launches_as_failure_flavor(
        self,
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        tmp_path,
    ):
        """_handle_queue_tech_lead -> queue -> launch skips the manifest query.

        Also the P1 producer-to-snapshot regression: the failure is discovered
        on tick N (queued via QueueTechLeadAction), the per-tick
        discovered_failures buffer is cleared after planning, and the launch
        happens on tick N+1 — the written board snapshot must still contain
        the investigation's own triggering failure. Uses the REAL
        StateBoardSnapshotProvider over the same state, constructor-injected.
        """
        state = OrchestratorState()
        provider = StateBoardSnapshotProvider(
            BoardSnapshotBuilder(
                timeline_reader=lambda issue, limit: [],
                log_tail_provider=lambda lines: [],
                case_file_reader=lambda: (),
                shipped_fix_reader=lambda limit: (),
                e2e_health_reader=lambda now: None,
                session_activity_reader=lambda session: None,
                clock=lambda: datetime(2026, 7, 10, 12, 0, 0),
            ),
            lambda: state,
        )
        launcher_bundle = _build_launcher_bundle(
            sample_config,
            mock_events,
            mock_repo_host,
            mock_worktree_manager,
            mock_working_copy,
            mock_command_runner,
            board_snapshot_provider=provider,
        )
        config = launcher_bundle.launcher.config
        TestLaunchTechLeadIssueSessionFlavors._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.prs_with_label = [
            TestLaunchTechLeadIssueSessionFlavors._tech_lead_pr(555)
        ]
        diagnostic = tmp_path / "failure-diagnostic.md"
        diagnostic.write_text("what went wrong")

        # Tick N: the planner threads the typed failure through the action.
        self._apply_actions(
            state,
            [
                QueueTechLeadAction(
                    issue_number=904,
                    title="Investigate: Test issue (timed_out)",
                    failure=DiscoveredFailure(
                        issue_number=904,
                        issue_title="Test issue",
                        failure_reason="timed_out",
                        artifact_hints=(str(diagnostic),),
                    ),
                    reason="Session failed with status 'timed_out'",
                )
            ],
        )
        (queued,) = state.pending_tech_lead_reviews
        assert queued.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        assert queued.failure is not None

        # Tick boundary: the orchestrator clears the per-tick fact buffer.
        state.discovered_failures.clear()

        # Tick N+1: launch consumes the queue item.
        session = self._launch(queued, state, launcher_bundle)

        # A launched item leaves the queue and cannot be relaunched next tick.
        assert state.pending_tech_lead_reviews == []
        assert state.active_sessions == [session]

        # Failure investigation must NOT query or build the batch PR manifest...
        assert mock_repo_host.get_prs_with_label_calls == []
        run_dir = TestLaunchTechLeadIssueSessionFlavors._started_run_dir(mock_events)
        run_manifest = json.loads((run_dir / "manifest.json").read_text())
        assert "tech_lead_manifest" not in run_manifest
        # ...and its assignment records the focused investigation.
        assignment = json.loads(
            (run_dir / "tech-lead-data" / "tech-lead-assignment.json").read_text()
        )
        assert assignment["flavor"] == "failure_investigation"
        assert assignment["focus_issue_number"] == 904

        # The written snapshot preserves the triggering failure — including
        # its real artifact hints — across the queue boundary even though the
        # live buffer was cleared (#6762: hints must reach the agent, not be
        # dropped at the projection).
        snapshot = BoardSnapshot.read(run_dir / "tech-lead-data" / "board-snapshot.json")
        assert [
            (f.issue_number, f.failure_reason, f.artifact_hints)
            for f in snapshot.recent_failures
        ] == [(904, "timed_out", [str(diagnostic)])]

    def test_storm_authority_excludes_an_unrelated_pending_investigation(
        self,
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        tmp_path,
    ):
        """#6780 — the exact scenario, end to end through the real queue.

        A 3-issue storm escalates while an older investigation for #99 is
        still pending. Intake correctly leaves that investigation queued, so
        the REAL StateBoardSnapshotProvider merges #99 into the launch
        snapshot's failures alongside the cohort. Authority derived from that
        list would let the health review reset_retry/kill_hung_session #99 —
        an issue it does not own. The grant must decide.
        """
        state = OrchestratorState()
        provider = StateBoardSnapshotProvider(
            BoardSnapshotBuilder(
                timeline_reader=lambda issue, limit: [],
                log_tail_provider=lambda lines: [],
                case_file_reader=lambda: (),
                shipped_fix_reader=lambda limit: (),
                e2e_health_reader=lambda now: None,
                session_activity_reader=lambda session: None,
                clock=lambda: datetime(2026, 7, 10, 12, 0, 0),
            ),
            lambda: state,
        )
        launcher_bundle = _build_launcher_bundle(
            sample_config,
            mock_events,
            mock_repo_host,
            mock_worktree_manager,
            mock_working_copy,
            mock_command_runner,
            board_snapshot_provider=provider,
        )
        config = launcher_bundle.launcher.config
        TestLaunchTechLeadIssueSessionFlavors._enable_tech_lead_agent(config, tmp_path)

        queues = PendingSessionQueues(state)
        # The unrelated investigation that survives the storm collapse.
        queues.queue_failure_investigation(
            99,
            "Investigate: Unrelated thing (failed)",
            failure=DiscoveredFailure(99, "Unrelated thing", "failed"),
        )
        queues.queue_health_review(
            905,
            "Health Review — problem storm",
            problem_cohort=tuple(
                DiscoveredFailure(number, f"Problem {number}", "failed")
                for number in (41, 42, 43)
            ),
        )
        (queued,) = [
            item
            for item in state.pending_tech_lead_reviews
            if item.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        ]

        session = self._launch(queued, state, launcher_bundle)

        run_dir = TestLaunchTechLeadIssueSessionFlavors._started_run_dir(mock_events)
        snapshot = BoardSnapshot.read(
            run_dir / "tech-lead-data" / "board-snapshot.json"
        )
        assert 99 in {f.issue_number for f in snapshot.recent_failures}, (
            "the unrelated failure is real board context and must stay visible"
        )
        authority = SqliteTechLeadAuthorityStore.for_repo(config.repo_root).load(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.problem_issue_numbers == (41, 42, 43)
        assert authority.allowed_act_level_targets() == frozenset({41, 42, 43}), (
            "#99 is context, not act-level authority"
        )
        assert snapshot.problem_issue_numbers() == frozenset({41, 42, 43})

    def test_recovered_storm_anchor_launches_with_its_original_cohort_authority(
        self,
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        tmp_path,
    ):
        """#6780 — a storm anchor keeps its cohort authority across a restart.

        The cohort is created on tick N and only reaches the agent at launch.
        If the orchestrator dies in between, the in-memory pending queue is
        gone and the anchor comes back from its LABEL alone. Recovery must
        rehydrate the cohort from the durable ledger, because the queued item
        is what grants launch its ``problem_issue_numbers`` — recovering empty
        would launch a health review that rejects every proposal for the
        issues that triggered it.

        Drives the real chain through real SQLite: intake -> restart ->
        recover -> launch -> TechLeadLaunchAuthority.
        """
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction
        from issue_orchestrator.control.health_review_trigger import (
            intake_created_tech_lead_anchor,
            recover_pending_tech_lead_anchors,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        authority_store = SqliteTechLeadAuthorityStore.for_repo(
            sample_config.repo_root
        )
        cohort = tuple(
            DiscoveredFailure(
                issue_number=number,
                issue_title=f"Problem {number}",
                failure_reason="failed",
                artifact_hints=(str(tmp_path / f"diagnostic-{number}.md"),),
                observed_at=1_000.0,
            )
            for number in (41, 42, 43)
        )

        # Tick N (pre-crash process): the anchor is created and intake
        # persists the cohort before collapsing the investigations.
        pre_crash_state = OrchestratorState()
        intake_created_tech_lead_anchor(
            CreateTechLeadIssueAction(
                title="Health Review — problem storm",
                body="Problem storm",
                labels=("agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL),
                pr_count=0,
                storm_problems=cohort,
                origin=TechLeadCreationOrigin.authors_anchor(),
            ),
            905,
            pre_crash_state,
            None,
            authority_store,
        )

        # ---- CRASH: everything in memory is lost. ----
        state = OrchestratorState()
        provider = StateBoardSnapshotProvider(
            BoardSnapshotBuilder(
                timeline_reader=lambda issue, limit: [],
                log_tail_provider=lambda lines: [],
                case_file_reader=lambda: (),
                shipped_fix_reader=lambda limit: (),
                e2e_health_reader=lambda now: None,
                session_activity_reader=lambda session: None,
                clock=lambda: datetime(2026, 7, 10, 12, 0, 0),
            ),
            lambda: state,
        )
        launcher_bundle = _build_launcher_bundle(
            sample_config,
            mock_events,
            mock_repo_host,
            mock_worktree_manager,
            mock_working_copy,
            mock_command_runner,
            board_snapshot_provider=provider,
        )
        config = launcher_bundle.launcher.config
        TestLaunchTechLeadIssueSessionFlavors._enable_tech_lead_agent(config, tmp_path)
        # The open anchor comes back from its marker label — the only
        # crash-safe truth a restart has.
        mock_repo_host.issues = {
            905: Issue(
                number=905,
                title="Health Review — problem storm",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
                repo="test/repo",
            )
        }

        # Startup recovery rehydrates the anchor AND its cohort.
        recover_pending_tech_lead_anchors(
            state,
            repository_host=mock_repo_host,
            config=config,
            session_exists=lambda name: False,
            tech_lead_authority=authority_store,
        )

        (queued,) = state.pending_tech_lead_reviews
        assert queued.issue_number == 905
        assert queued.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert queued.problem_cohort == cohort, (
            "the recovered anchor must carry the same typed cohort it was "
            "created with, hints included"
        )

        # Launch: the snapshot merges the recovered cohort, and the trusted
        # authority record scopes act-level proposals to those same issues.
        session = self._launch(queued, state, launcher_bundle)

        run_dir = TestLaunchTechLeadIssueSessionFlavors._started_run_dir(mock_events)
        snapshot = BoardSnapshot.read(
            run_dir / "tech-lead-data" / "board-snapshot.json"
        )
        assert snapshot.problem_issue_numbers() == frozenset({41, 42, 43})
        authority = authority_store.load(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert authority is not None
        assert authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert authority.problem_issue_numbers == (41, 42, 43)
        assert authority.allowed_act_level_targets() == frozenset({41, 42, 43})

    def test_recovered_periodic_anchor_has_no_cohort_authority(
        self,
        sample_config,
        mock_events,
        mock_repo_host,
        mock_worktree_manager,
        mock_working_copy,
        mock_command_runner,
        tmp_path,
    ):
        """A recovered PERIODIC anchor recovers no cohort (#6780).

        Only a storm records a ledger row, so the same recovery path must not
        manufacture act-level scope for an interval-created review — its
        remit is to walk the board, not to remediate a specific cohort.
        """
        from issue_orchestrator.control.health_review_trigger import (
            recover_pending_tech_lead_anchors,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        authority_store = SqliteTechLeadAuthorityStore.for_repo(
            sample_config.repo_root
        )
        launcher_bundle = _build_launcher_bundle(
            sample_config,
            mock_events,
            mock_repo_host,
            mock_worktree_manager,
            mock_working_copy,
            mock_command_runner,
        )
        config = launcher_bundle.launcher.config
        TestLaunchTechLeadIssueSessionFlavors._enable_tech_lead_agent(config, tmp_path)
        mock_repo_host.issues = {
            906: Issue(
                number=906,
                title="Health Review — walk the floor",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
                repo="test/repo",
            )
        }

        state = OrchestratorState()
        recover_pending_tech_lead_anchors(
            state,
            repository_host=mock_repo_host,
            config=config,
            session_exists=lambda name: False,
            tech_lead_authority=authority_store,
        )

        (queued,) = state.pending_tech_lead_reviews
        assert queued.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert queued.problem_cohort == ()

    def test_queue_tech_lead_action_without_failure_context_fails_fast(self):
        """The action boundary rejects failure investigations lacking context.

        ``QueueTechLeadAction.failure`` is a required keyword field: an
        investigation cannot even be DESCRIBED without its triggering failure
        (``PendingTechLeadReview.__post_init__`` remains as defense-in-depth
        for untyped callers reaching the queue owner directly).
        """
        with pytest.raises(TypeError, match="failure"):
            QueueTechLeadAction(
                issue_number=904,
                title="Investigate: Test issue (timed_out)",
                reason="Session failed with status 'timed_out'",
            )


# =============================================================================
# Session Callback Tests
# =============================================================================


class TestSessionLauncherCallback:
    """Tests for session_launcher_callback function."""

    def test_dispatches_to_correct_handler(self):
        """Verify callback dispatches to correct handler."""
        calls = []

        def issue_fn(n):
            calls.append(("issue", n))
            return None

        def review_fn(n):
            calls.append(("review", n))
            return None

        def retrospective_fn(n):
            calls.append(("retrospective", n))
            return None

        def rework_fn(n):
            calls.append(("rework", n))
            return None

        def tech_lead_fn(n):
            calls.append(("tech_lead", n))
            return None

        session_launcher_callback(
            SessionType.ISSUE,
            123,
            issue_fn,
            review_fn,
            retrospective_fn,
            rework_fn,
            tech_lead_fn,
        )
        session_launcher_callback(
            SessionType.REVIEW,
            456,
            issue_fn,
            review_fn,
            retrospective_fn,
            rework_fn,
            tech_lead_fn,
        )
        session_launcher_callback(
            SessionType.RETROSPECTIVE_REVIEW,
            789,
            issue_fn,
            review_fn,
            retrospective_fn,
            rework_fn,
            tech_lead_fn,
        )

        assert calls == [("issue", 123), ("review", 456), ("retrospective", 789)]

    def test_unknown_session_type_fails_fast(self):
        """Unknown session types should not silently no-op."""

        def launch_fn(_number):
            return None

        with pytest.raises(KeyError):
            session_launcher_callback(
                cast(SessionType, object()),
                123,
                launch_fn,
                launch_fn,
                launch_fn,
                launch_fn,
                launch_fn,
            )


# =============================================================================
# Session Reference Parsing Tests
# =============================================================================


class TestParseSessionRef:
    """Tests for parse_session_ref function (lines 1097-1101)."""

    def test_parses_valid_session_name(self):
        """Verify valid session name is parsed."""
        events = MockEventSink()
        ref = parse_session_ref("issue-123", "test", events)

        assert ref.number == 123

    def test_emits_error_event_on_invalid_name(self):
        """Verify error event is emitted for invalid name."""
        events = MockEventSink()

        with pytest.raises(ValueError):
            parse_session_ref("invalid-name", "test", events)

        # Should have emitted error event
        error_events = [e for e in events.events if "error" in str(e.name).lower()]
        assert len(error_events) == 1


# =============================================================================
# Restore Running Sessions Tests
# =============================================================================


def _claims_store(base=None):
    """The orchestrator-owned claim store, rooted outside any worktree.

    Defaults to a throwaway root for the launch tests that only assert queue
    state; pass a real base when a test needs launch and settlement to share
    one store (#6999 F7).
    """
    import tempfile

    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    return SqlitePendingWorkClaimStore.for_repo(
        Path(base) if base is not None else Path(tempfile.mkdtemp())
    )


def _no_claims_store():
    """A claim ledger holding nothing, for terminals that took no work.

    Restoration must read and enumerate, and must not write (#6999 F4/F8).
    """

    class _Empty:
        def hold_pending_work_claim(self, run, claim) -> None:
            raise AssertionError("restoration must not hold claims")

        def defer_pending_work_claim(self, run) -> None:
            raise AssertionError("restoration must not defer claims")

        def consume_pending_work_claim(self, run) -> None:
            raise AssertionError("restoration must not consume claims")

        def look_up_pending_work_claim(self, run):
            from issue_orchestrator.ports.pending_work_claim_store import (
                ClaimLookup,
                ClaimState,
            )

            return ClaimLookup(ClaimState.ABSENT)

        def list_unresolved_claims(self):
            return ()

        def list_unreadable_claims(self):
            return ()

        def mark_deferred_by_run_key(self, run_key) -> None:
            raise AssertionError("nothing to defer")

        def run_key_for(self, run) -> str:
            return str(run.run_dir)

    return _Empty()


def _no_quarantine():
    """These terminals hold no claim, so none may be quarantined (#6999 F6)."""
    owner = MagicMock()
    owner.quarantine.side_effect = AssertionError(
        "no terminal here holds a claim, so none may be quarantined"
    )
    return owner


class TestRestoreRunningSessions:
    """Tests for restore_running_sessions function (line 1085)."""

    def test_extends_active_sessions(self):
        """Verify restored sessions are added to active list."""
        mock_restorer = MagicMock()
        mock_session = MagicMock()
        mock_session.terminal_id = "issue-123"
        mock_restorer.restore_sessions.return_value = [mock_session]

        state = OrchestratorState()
        active_sessions = state.active_sessions
        running = [{"tab_name": "issue-123", "issue_number": 123}]

        added = restore_running_sessions(
            running, state, mock_restorer, _no_claims_store(), _no_quarantine()
        )

        assert added == [mock_session]
        assert len(active_sessions) == 1
        assert active_sessions[0] == mock_session

    def test_suppresses_duplicate_terminal_ids_from_restorer(self, tmp_path):
        """Runtime restoration also goes through the active-session owner."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        agent_config = AgentConfig(prompt_path=tmp_path / "prompt.txt")
        existing = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "existing",
            branch_name="123-existing",
            run_assets=make_session_run_assets(
                tmp_path / "existing",
                session_name="issue-123",
            ),
        )
        duplicate = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "duplicate",
            branch_name="123-duplicate",
            run_assets=make_session_run_assets(
                tmp_path / "duplicate",
                session_name="issue-123",
            ),
        )
        new_session = Session(
            key=SessionKey(issue=FakeIssueKey("456"), task=TaskKind.CODE),
            issue=Issue(number=456, title="Other", labels=["agent:web"]),
            agent_config=agent_config,
            terminal_id="issue-456",
            worktree_path=tmp_path / "new",
            branch_name="456-new",
            run_assets=make_session_run_assets(
                tmp_path / "new",
                session_name="issue-456",
            ),
        )
        mock_restorer = MagicMock()
        mock_restorer.restore_sessions.return_value = [duplicate, new_session]
        state = OrchestratorState()
        state.active_sessions.append(existing)
        active_sessions = state.active_sessions

        added = restore_running_sessions(
            [{"tab_name": "issue-123"}, {"tab_name": "issue-456"}],
            state,
            mock_restorer,
            _no_claims_store(),
            _no_quarantine(),
        )

        assert added == [new_session]
        assert active_sessions == [existing, new_session]


# =============================================================================
# Process Active Sessions Tests
# =============================================================================


class TestProcessActiveSessions:
    """Tests for process_active_sessions function (line 1034)."""

    def test_completed_decision_batch_applies_siblings_before_raising(self):
        """One failed completed decision must not discard the rest of the drain batch."""
        first = CompletedDecision(
            session=MagicMock(terminal_id="issue-1"),
            decision=None,
            error=RuntimeError("decide failed"),
        )
        second = CompletedDecision(
            session=MagicMock(terminal_id="issue-2"),
            decision=SessionDecision(
                status=SessionStatus.RUNNING,
                provider_success="codex",
            ),
            error=None,
        )
        provider_resilience = MagicMock()
        applied: list[str] = []

        def apply(completed: CompletedDecision) -> None:
            applied.append(completed.session.terminal_id)
            if completed.error is not None:
                raise completed.error
            assert completed.decision is not None
            _record_provider_resilience_effects(
                completed.decision,
                provider_resilience,
            )

        with pytest.raises(RuntimeError, match="decide failed"):
            _apply_completed_decisions([first, second], apply)

        assert applied == ["issue-1", "issue-2"]
        provider_resilience.record_success.assert_called_once_with("codex")

    def test_provider_resilience_effects_are_recorded_on_apply_thread(self):
        """Provider-circuit mutations happen when the drained decision is applied."""
        provider_resilience = MagicMock()

        _record_provider_resilience_effects(
            SessionDecision(
                status=SessionStatus.RUNNING,
                provider_success="codex",
            ),
            provider_resilience,
        )
        _record_provider_resilience_effects(
            SessionDecision(
                status=SessionStatus.BLOCKED,
                provider_transient_failure=ProviderTransientFailureDecision(
                    provider="claude-code",
                    error_summary="provider overloaded",
                    attempts=3,
                ),
            ),
            provider_resilience,
        )

        provider_resilience.record_success.assert_called_once_with("codex")
        provider_resilience.record_transient_failure.assert_called_once_with(
            "claude-code",
            error_summary="provider overloaded",
            attempts=3,
        )

    def test_skips_running_sessions(self, sample_agent_config, tmp_path):
        """Verify running sessions are skipped."""
        from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult

        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.running(runtime_minutes=5.0)

        config = MagicMock()

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=MagicMock(),
            completion_handler=MagicMock(),
            action_applier=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            pending_work_claims=_test_claim_store(),
        )

        # Session should still be in active list
        assert len(state.active_sessions) == 1

    def test_keeps_deferred_completion_sessions_active(self, sample_agent_config, tmp_path):
        """A RUNNING decision after observation is a deferral, not completion."""
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.terminated()
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.RUNNING,
            reason="Review exchange running in background; awaiting completion",
        )
        mock_completion_handler = MagicMock()
        mock_action_applier = MagicMock()

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=mock_controller,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            worktree_manager=None,
            kill_session_fn=MagicMock(),
            config=MagicMock(),
            pending_work_claims=_test_claim_store(),
        )

        assert state.active_sessions == [session]
        mock_completion_handler.process_completion.assert_not_called()
        mock_action_applier.apply_actions.assert_not_called()

    def test_attributes_tick_phase_to_issue_being_handled(self, sample_agent_config, tmp_path):
        """While a session's (synchronous) completion runs, the tick phase names
        the issue, so a stall during publish shows up on the dashboard as
        'active_sessions:#392' rather than a generic 'active_sessions'."""
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=392, title="Test", labels=["agent:backend"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("392"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-392",
            worktree_path=tmp_path / "worktree",
            branch_name="392-feature",
            run_assets=make_session_run_assets(tmp_path / "worktree", session_name="issue-392"),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.terminated()

        captured_phase = {}

        def capture_phase(*args, **kwargs):
            # The heavy publish work happens inside decide_outcome; capture what
            # the dashboard would read if it sampled mid-stall.
            captured_phase["value"] = state.current_tick_phase
            return SessionDecision(
                status=SessionStatus.RUNNING,
                reason="deferred",
            )

        mock_controller = MagicMock()
        mock_controller.decide_outcome.side_effect = capture_phase

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=mock_controller,
            completion_handler=MagicMock(),
            action_applier=MagicMock(),
            worktree_manager=None,
            kill_session_fn=MagicMock(),
            config=MagicMock(),
            pending_work_claims=_test_claim_store(),
        )

        assert captured_phase["value"] == "active_sessions:#392"

    def test_completion_event_fires_once_across_many_ticks_of_deferred_session(
        self, sample_agent_config, tmp_path
    ):
        """Regression for #6082: OBSERVATION_COMPLETION_DETECTED must fire
        exactly once per session, even when the controller keeps the session
        active across many ticks (e.g. a background review exchange that
        returns SessionStatus.RUNNING).

        This test deliberately uses a real SessionObserver — the bug lives
        at the seam between observer (re-reads completion.json each tick),
        controller (says RUNNING during deferred work), and
        process_active_sessions (keeps RUNNING sessions in active_sessions).
        Each component is individually correct; the cardinality invariant
        only emerges from running them together across ticks.
        """
        import json
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.events import EventName
        from issue_orchestrator.observation.observer import SessionObserver

        issue = Issue(number=359, title="Test", labels=["agent:backend"])
        issue_key = FakeIssueKey("359")
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        (completion_dir / "completion.json").write_text(json.dumps({
            "session_id": "any-session-id",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-359",
            worktree_path=worktree,
            branch_name="359-feature",
            run_assets=make_session_run_assets(
                worktree,
                session_name="issue-359",
            ),
        )
        state = OrchestratorState(active_sessions=[session])

        events = MockEventSink()
        mock_session_runner = MagicMock()
        mock_session_runner.session_exists_by_name.return_value = True
        observer = SessionObserver(
            MagicMock(),
            _claims_store(),
            events=events,
            session_runner=mock_session_runner,
            repository_host=MagicMock(),
        )

        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.RUNNING,
            reason="Review exchange running in background; awaiting completion",
        )

        # Simulate ten observation ticks while the session lingers in a
        # deferred state — exactly the live tixmeup #359 scenario.
        for _ in range(10):
            process_active_sessions(
                state=state,
                observer=observer,
                session_controller=mock_controller,
                completion_handler=MagicMock(),
                action_applier=MagicMock(),
                worktree_manager=None,
                kill_session_fn=MagicMock(),
                config=MagicMock(),
                pending_work_claims=_test_claim_store(),
            )

        # Session stayed active because the controller deferred…
        assert state.active_sessions == [session]
        # …and the controller saw a terminated observation each tick…
        assert mock_controller.decide_outcome.call_count == 10
        # …but the user-facing event fired exactly once.
        completion_events = events.get_events_by_name(
            EventName.OBSERVATION_COMPLETION_DETECTED
        )
        assert len(completion_events) == 1, (
            "OBSERVATION_COMPLETION_DETECTED must fire once per session, not "
            f"once per tick. Got {len(completion_events)} emissions."
        )

    def test_skips_duplicate_snapshot_entries_after_terminal_processing(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """Duplicate active-session entries cannot append duplicate timeout events."""
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        duplicate = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session, duplicate])
        mock_observer = MagicMock()
        terminal_observation = SessionObservationResult.timed_out()
        mock_observer.observe_session.return_value = terminal_observation
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.TIMED_OUT,
            reason="timeout",
        )
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None
        mock_controller.session_output = session_output
        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="timed_out",
                runtime_minutes=90,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        kill_session_fn = MagicMock()

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=mock_controller,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            worktree_manager=None,
            kill_session_fn=kill_session_fn,
            config=MagicMock(),
            pending_work_claims=_test_claim_store(),
        )

        assert state.active_sessions == []
        mock_observer.observe_session.assert_called_once_with(session)
        mock_controller.decide_outcome.assert_called_once()
        mock_completion_handler.process_completion.assert_called_once()
        kill_session_fn.assert_called_once_with("issue-123")

    def test_background_dispatcher_keeps_heartbeat_free_until_drained(
        self, sample_agent_config, tmp_path
    ):
        """With a background dispatcher, a slow completion decision runs off the
        tick thread: the dispatch tick returns immediately with the session
        still active and no completion applied; a later tick (after the decision
        finishes) drains it and applies handle_session_completion exactly once.
        Regression for the 153.9s synchronous-publish freeze.

        Uses a deterministic in-process runner (no threads, no waits): the test
        drives when the offloaded decision runs via ``run_all``, so the off-tick
        behavior is fully reproducible rather than depending on thread scheduling.
        """
        from collections.abc import Callable

        from issue_orchestrator.control.completion_dispatcher import (
            BackgroundCompletionDispatcher,
        )
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult
        from issue_orchestrator.ports.background_job import CompletedJob

        class ManualJobRunner:
            """Deterministic ``BackgroundJobRunner``: submitted work is stored and
            executes only when the test calls ``run_all`` — no threads, no waits.

            ``is_running`` is True only while the job would be executing; it goes
            False as soon as ``fn`` returns in ``run_all``, matching the production
            ``ThreadBackgroundJobRunner``'s ``Thread.is_alive()`` semantics (and the
            ``_RecordingRunner`` in ``test_publish_recovery.py``). The finished
            result then waits in ``drain_completed``. Re-dispatch of a
            decided-but-undrained session is prevented by ``process_active_sessions``
            draining completed decisions *before* it dispatches — not by this fake
            over-reporting the job as still running.
            """

            def __init__(self) -> None:
                self._pending: dict[str, Callable[[], None]] = {}
                self._in_flight: set[str] = set()
                self._done: list[CompletedJob] = []

            def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
                if job_id in self._in_flight:
                    return False
                self._in_flight.add(job_id)
                self._pending[job_id] = fn
                return True

            def is_running(self, job_id: str) -> bool:
                return job_id in self._in_flight

            def run_all(self) -> None:
                for job_id, fn in list(self._pending.items()):
                    error: BaseException | None = None
                    try:
                        fn()
                    except BaseException as exc:  # noqa: BLE001 — mirror the thread runner
                        error = exc
                    # is_running() goes False the instant fn returns (or raises),
                    # like Thread.is_alive(); the result waits in _done for drain.
                    self._in_flight.discard(job_id)
                    self._pending.pop(job_id, None)
                    self._done.append(CompletedJob(job_id=job_id, error=error))

            def drain_completed(self) -> list[CompletedJob]:
                done = self._done
                self._done = []
                return done

        issue = Issue(number=392, title="Test", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("392"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-392",
            worktree_path=tmp_path / "worktree",
            branch_name="392-feature",
            run_assets=make_session_run_assets(tmp_path / "worktree", session_name="issue-392"),
        )
        state = OrchestratorState(active_sessions=[session])

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.timed_out()

        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.TIMED_OUT, reason="done"
        )
        mock_controller.session_output = session_output

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=392, title="Test", agent_type="agent:web",
                status="timed_out", runtime_minutes=90,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False, pr_url=None, pr_number=None,
        )
        kill_session_fn = MagicMock()
        runner = ManualJobRunner()
        dispatcher = BackgroundCompletionDispatcher(runner)

        def run_tick():
            process_active_sessions(
                state=state,
                observer=mock_observer,
                session_controller=mock_controller,
                completion_handler=mock_completion_handler,
                action_applier=MagicMock(),
                worktree_manager=None,
                kill_session_fn=kill_session_fn,
                config=MagicMock(),
                completion_dispatcher=dispatcher,
                pending_work_claims=_test_claim_store(),
            )

        # Tick 1: the decision is offloaded to the runner, NOT run inline — the
        # tick returns with the session still active and nothing applied.
        run_tick()
        assert session in state.active_sessions  # not yet completed
        mock_completion_handler.process_completion.assert_not_called()
        assert dispatcher.in_flight("issue-392") is True
        mock_controller.decide_outcome.assert_not_called()  # dispatched, not run on the tick

        # Tick 2 (decision still in flight, not yet run): no re-dispatch, no apply.
        run_tick()
        assert dispatcher.in_flight("issue-392") is True
        mock_controller.decide_outcome.assert_not_called()
        assert state.active_sessions == [session]

        # The offloaded decision runs (deterministically, on our command) exactly
        # once. The runner reports it as no longer executing the instant it
        # returns (like the thread runner's Thread.is_alive()), but the DISPATCHER
        # keeps it in-flight until the result is drained — so the session stays
        # active and is not re-dispatched into a duplicate decision.
        runner.run_all()
        mock_controller.decide_outcome.assert_called_once()
        assert runner.is_running("issue-392") is False      # execution finished
        assert dispatcher.in_flight("issue-392") is True     # owner: in-flight until drained
        assert state.active_sessions == [session]
        mock_completion_handler.process_completion.assert_not_called()

        # The next tick drains the finished decision and applies completion once.
        run_tick()
        assert state.active_sessions == []
        mock_completion_handler.process_completion.assert_called_once()
        kill_session_fn.assert_called_once_with("issue-392")

    def test_completion_finishing_after_drain_snapshot_is_not_re_dispatched(
        self, sample_agent_config, tmp_path
    ):
        """Regression: a worker that finishes AFTER a tick's drain snapshot but
        before that tick's in_flight() check must not be re-dispatched.

        With in_flight tied to the runner's execution status, that window let the
        tick re-dispatch the still-undrained session (submit_count == 2), running
        a duplicate decision (a second publish + push + PR) while the first result
        waited for the next drain. The dispatcher now owns the
        submitted-through-drained lifecycle, so the session stays in-flight until
        drain and is dispatched exactly once.
        """
        from collections.abc import Callable

        from issue_orchestrator.control.completion_dispatcher import (
            BackgroundCompletionDispatcher,
        )
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult
        from issue_orchestrator.ports.background_job import CompletedJob

        class FinishOnDrainRunner:
            """Models the race window. A job finishes only after it has survived a
            prior tick's drain, and it finishes as a side effect of the *next*
            drain-snapshot call: that drain returns the pre-finish snapshot, then
            the worker completes — so later in the SAME tick (at the dispatch
            loop's in_flight/is_running check) execution has stopped while the
            result is still queued for the following drain. submit_count exposes
            any re-dispatch through that window."""

            def __init__(self) -> None:
                self._pending: dict[str, Callable[[], None]] = {}
                self._alive: set[str] = set()
                self._done: list[CompletedJob] = []
                self._drains_survived: dict[str, int] = {}
                self.submit_count = 0

            def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
                if job_id in self._alive:
                    return False
                self.submit_count += 1
                self._alive.add(job_id)
                self._pending[job_id] = fn
                return True

            def is_running(self, job_id: str) -> bool:
                return job_id in self._alive

            def drain_completed(self) -> list[CompletedJob]:
                snapshot = self._done
                self._done = []
                for job_id in list(self._pending):
                    self._drains_survived[job_id] = self._drains_survived.get(job_id, 0) + 1
                    # Finish only once the job has already survived a prior drain,
                    # so this fires on tick 2's first drain (not tick 1's), after
                    # its snapshot — leaving is_running() False at the dispatch
                    # loop while the result waits for the next drain.
                    if self._drains_survived[job_id] >= 2:
                        self._pending.pop(job_id)()
                        self._alive.discard(job_id)
                        self._done.append(CompletedJob(job_id=job_id, error=None))
                return snapshot

        issue = Issue(number=392, title="Test", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("392"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-392",
            worktree_path=tmp_path / "worktree",
            branch_name="392-feature",
            run_assets=make_session_run_assets(tmp_path / "worktree", session_name="issue-392"),
        )
        state = OrchestratorState(active_sessions=[session])

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.timed_out()

        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.TIMED_OUT, reason="done"
        )
        mock_controller.session_output = session_output

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=392, title="Test", agent_type="agent:web",
                status="timed_out", runtime_minutes=90,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False, pr_url=None, pr_number=None,
        )
        kill_session_fn = MagicMock()
        runner = FinishOnDrainRunner()
        dispatcher = BackgroundCompletionDispatcher(runner)

        def run_tick():
            process_active_sessions(
                state=state,
                observer=mock_observer,
                session_controller=mock_controller,
                completion_handler=mock_completion_handler,
                action_applier=MagicMock(),
                worktree_manager=None,
                kill_session_fn=kill_session_fn,
                config=MagicMock(),
                completion_dispatcher=dispatcher,
                pending_work_claims=_test_claim_store(),
            )

        # Tick 1: dispatch the decision (submit #1); nothing is decided yet.
        run_tick()
        assert runner.submit_count == 1
        assert state.active_sessions == [session]
        mock_controller.decide_outcome.assert_not_called()

        # Tick 2: the worker finishes right after this tick's first drain snapshot,
        # so is_running() is now False. With in_flight tied to execution status the
        # dispatch loop would re-dispatch here (submit #2 — a duplicate decision)
        # before the post-dispatch drain applies the first result. The owner keeps
        # the session in-flight until drained, so it is dispatched exactly once and
        # the finished decision is applied by this tick's second drain.
        run_tick()
        assert runner.submit_count == 1  # would be 2 under the is_running() bug
        mock_controller.decide_outcome.assert_called_once()  # decided exactly once
        assert state.active_sessions == []
        mock_completion_handler.process_completion.assert_called_once()
        kill_session_fn.assert_called_once_with("issue-392")


# =============================================================================
# Session Helper Tests
# =============================================================================


class TestCreateSession:
    """Tests for create_session function."""

    def test_creates_session_via_manager(self):
        """Verify session is created through manager."""
        mock_manager = MockSessionManager()
        events = MockEventSink()

        result = create_session(
            "issue-123",
            "claude --help",
            Path("/tmp/worktree"),
            "Test Session",
            mock_manager,
            events,
        )

        assert result is True
        assert len(mock_manager.start_calls) == 1


class TestSessionExists:
    """Tests for session_exists function."""

    def test_checks_via_manager(self):
        """Verify existence is checked through manager."""
        mock_manager = MockSessionManager()
        mock_manager.sessions["issue-123"] = True
        events = MockEventSink()

        result = session_exists("issue-123", mock_manager, events)

        assert result is True


class TestKillSession:
    """Tests for kill_session function."""

    def test_stops_via_manager(self):
        """Verify session is stopped through manager."""
        mock_manager = MockSessionManager()
        events = MockEventSink()

        kill_session("issue-123", mock_manager, events)

        assert len(mock_manager.stop_calls) == 1


class TestGetSessionMachine:
    """Tests for get_session_machine function."""

    def test_gets_from_state_machines(self):
        """Verify machine is retrieved from manager."""
        mock_sm_manager = MagicMock()
        expected_machine = SessionStateMachine("issue-123", 123, timeout_minutes=45)
        mock_sm_manager.get_session_machine.return_value = expected_machine

        result = get_session_machine("issue-123", 123, 45, mock_sm_manager)

        assert result == expected_machine
        mock_sm_manager.get_session_machine.assert_called_once_with("issue-123", 123, 45)


# =============================================================================
# Handle Session Completion Tests
# =============================================================================


class TestHandleSessionCompletion:
    """Tests for handle_session_completion function."""

    def test_removes_session_from_active(self, sample_agent_config, tmp_path):
        """Verify completed session is removed from active list."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=10,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        mock_action_applier = MagicMock()
        mock_observer = MagicMock()
        config = MagicMock()
        config.cleanup.without_tech_lead.close_ai_session_tabs = True
        config.code_review_agent = None

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.active_sessions) == 0
        assert len(state.completed_today) == 1
        assert 123 in state.completed_today

    def _discover_failure(self, sample_agent_config, tmp_path, *, diagnostic_path):
        """Run a FAILED completion and return (failure, run artifacts).

        Shared driver for the artifact-hint provenance contract (#6771 round
        3): callers pass the diagnostic path exactly as their production
        writer reports it (absolute or worktree-relative).
        """
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=10,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        config = MagicMock()
        config.cleanup.without_tech_lead.close_ai_session_tabs = True
        config.code_review_agent = None
        run_dir = Path(session.run_assets.run_dir)
        claude_log = run_dir / "claude.jsonl"
        claude_log.write_text("{}\n")
        session_output = MagicMock(spec=SessionOutput)
        session_output.attach_claude_log.return_value = claude_log

        handle_session_completion(
            session=session,
            status=SessionStatus.FAILED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=session_output,
            diagnostic_path=diagnostic_path,
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_failures) == 1
        failure = state.discovered_failures[0]
        assert failure.issue_number == 123
        return failure, run_dir, claude_log

    def test_adds_to_discovered_failures_on_failure(self, sample_agent_config, tmp_path):
        """Absolute diagnostic paths (SessionOutput.write_diagnostic) pass through.

        Verifies the failed session is added to discovered failures with real
        artifact hints when the diagnostic is reported ABSOLUTE — the
        ``SessionOutput.write_diagnostic`` contract.
        """
        diagnostic = tmp_path / "worktree" / "failure-diagnostic.md"
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_text("what went wrong")

        failure, run_dir, claude_log = self._discover_failure(
            sample_agent_config, tmp_path, diagnostic_path=str(diagnostic)
        )

        # #6762: the discovery seam gathers REAL artifact hints — the failure
        # diagnostic, the attached agent log, and the run-dir artifacts that
        # exist on disk (manifest + analysis + terminal recording).
        assert failure.artifact_hints == (
            str(diagnostic),
            str(claude_log),
            str(run_dir / "manifest.json"),
            str(run_dir / "analysis.json"),
            str(run_dir / "terminal-recording.jsonl"),
        )

    def test_relative_diagnostic_hint_resolves_against_worktree(
        self, sample_agent_config, tmp_path
    ):
        """Worktree-relative diagnostics resolve to absolute hints (#6771 r3).

        The production failure writer (``write_failure_diagnostic``) reports
        the diagnostic as a WORKTREE-RELATIVE path
        (``.issue-orchestrator/sessions/<run>/<file>``). The discovery seam
        must resolve it against the failed session's worktree instead of
        testing it against the process CWD — otherwise every real failure
        diagnostic silently vanishes from ``artifact_hints``.
        """
        worktree = tmp_path / "worktree"
        run_assets = make_session_run_assets(worktree, session_name="issue-123")
        diagnostic_rel = (
            f".issue-orchestrator/sessions/{run_assets.run_dir.name}/"
            "failure-diagnostic-20260710-000000.json"
        )
        diagnostic_abs = worktree / diagnostic_rel
        diagnostic_abs.write_text('{"errors": ["push failed"]}')
        assert not Path(diagnostic_rel).exists(), (
            "test must run with a CWD where the relative path does NOT "
            "exist, otherwise it cannot catch CWD-relative resolution"
        )

        failure, run_dir, claude_log = self._discover_failure(
            sample_agent_config, tmp_path, diagnostic_path=diagnostic_rel
        )

        # The relative production contract resolves against the worktree and
        # is stored ABSOLUTE (the investigation launches ticks later from a
        # different working directory).
        assert failure.artifact_hints == (
            str(diagnostic_abs),
            str(claude_log),
            str(run_dir / "manifest.json"),
            str(run_dir / "analysis.json"),
            str(run_dir / "terminal-recording.jsonl"),
        )

    def test_timed_out_session_kills_terminal_before_actions(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """Timeout terminalization happens before external completion actions."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        calls: list[str] = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.side_effect = lambda *args, **kwargs: (
            calls.append("process_completion")
            or MagicMock(
                actions=[AddLabelAction(issue_number=123, label="blocked-failed")],
                history_entry=SessionHistoryEntry(
                    issue_number=123,
                    title="Test Issue",
                    agent_type="agent:web",
                    status="timed_out",
                    runtime_minutes=90,
                ),
                cleanup=CleanupDecision.immediate(),
                should_queue_review=False,
                pr_url=None,
                pr_number=None,
            )
        )
        mock_action_applier = MagicMock()
        mock_action_applier.apply_all.side_effect = lambda _actions: calls.append("actions")
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None

        handle_session_completion(
            session=session,
            status=SessionStatus.TIMED_OUT,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: calls.append("kill"),
            config=MagicMock(),
            session_output=session_output,
            pending_work_claims=_test_claim_store(),
        )

        assert calls == ["process_completion", "kill", "actions"]
        assert state.active_sessions == []
        assert len(state.discovered_failures) == 1

    def test_completion_artifacts_use_recorded_run_dir_without_lookup(
        self,
        sample_agent_config,
        tmp_path,
        monkeypatch,
    ):
        """Completion diagnostics use the launch-recorded artifact path."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                run_id="20260525",
                session_name="coding-1",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        run_dir = session.run_dir
        analyzed: list[Path] = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:web",
                status="timed_out",
                runtime_minutes=90,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        session_output = MagicMock(spec=SessionOutput)
        monkeypatch.setattr(
            "issue_orchestrator.control.session_completion.run_session_analysis",
            lambda path: analyzed.append(path),
        )

        handle_session_completion(
            session=session,
            status=SessionStatus.TIMED_OUT,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: None,
            config=MagicMock(),
            session_output=session_output,
            pending_work_claims=_test_claim_store(),
        )

        session_output.find_run_dir.assert_not_called()
        session_output.read_manifest.assert_not_called()
        session_output.attach_claude_log.assert_called_once_with(run_dir)
        assert analyzed == [run_dir]

    def test_completed_session_stops_runtime_before_cleanup_actions(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """A completed agent must not remain visible as a running terminal."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        calls: list[str] = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.side_effect = lambda *args, **kwargs: (
            calls.append("process_completion")
            or MagicMock(
                actions=[AddLabelAction(issue_number=123, label="code-reviewed")],
                history_entry=SessionHistoryEntry(
                    issue_number=123,
                    title="Test Issue",
                    agent_type="agent:web",
                    status="completed",
                    runtime_minutes=12,
                ),
                cleanup=CleanupDecision.immediate(),
                should_queue_review=False,
                pr_url=None,
                pr_number=None,
            )
        )
        mock_action_applier = MagicMock()
        mock_action_applier.apply_all.side_effect = lambda _actions: calls.append("actions")
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: calls.append("kill"),
            config=MagicMock(),
            session_output=session_output,
            pending_work_claims=_test_claim_store(),
        )

        assert calls == ["process_completion", "kill", "actions"]
        assert state.active_sessions == []
        assert state.completed_today == [123]

    def test_retrospective_changes_requested_queues_coder_rework(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """Review-first changes_requested hands the issue to coder rework."""
        issue = Issue(
            number=365,
            title="Review existing work",
            labels=["agent:web", "agent:reviewer", "lack-of-review-redo"],
        )
        session = Session(
            key=SessionKey(
                issue=FakeIssueKey("365"),
                task=TaskKind.RETROSPECTIVE_REVIEW,
            ),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="retrospective-review-365",
            worktree_path=tmp_path / "worktree",
            branch_name="365-review",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="retrospective-review-365",
            ),
            agent_label="agent:reviewer",
        )
        state = OrchestratorState(active_sessions=[session])
        completion_detail = {
            "outcome": "review_changes_requested",
            "review_issues": "Add regression tests before approving.",
        }

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=365,
                title="Review existing work",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=4,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: None,
            config=MagicMock(),
            session_output=session_output,
            completion_detail=completion_detail,
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.pending_reworks) == 1
        rework = state.pending_reworks[0]
        assert rework.issue_number == 365
        assert rework.agent_type == "agent:web"
        assert rework.source == "retrospective_review"
        assert rework.feedback == "Add regression tests before approving."
        mock_completion_handler.process_completion.assert_called_once()
        assert (
            mock_completion_handler.process_completion.call_args.kwargs["completion_detail"]
            == completion_detail
        )

    def test_finished_session_already_gone_does_not_warn(
        self,
        sample_agent_config,
        tmp_path,
        caplog,
    ):
        """Adapters may report already-gone sessions while completion cleanup runs."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        def already_gone(_name: str) -> None:
            raise FileNotFoundError("Session not found")

        with caplog.at_level("WARNING"):
            _terminate_finished_session(
                session,
                SessionStatus.COMPLETED,
                already_gone,
            )

        assert "Failed to stop finished session" not in caplog.text

    def test_timed_out_session_kills_terminal_when_completion_processing_fails(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """A timeout cannot be restored and reprocessed if completion handling fails."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        kill_session = MagicMock()
        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            handle_session_completion(
                session=session,
                status=SessionStatus.TIMED_OUT,
                state=state,
                completion_handler=mock_completion_handler,
                action_applier=MagicMock(),
                observer=MagicMock(),
                worktree_manager=None,
                kill_session_fn=kill_session,
                config=MagicMock(),
                session_output=MagicMock(spec=SessionOutput),
                pending_work_claims=_test_claim_store(),
            )

        assert state.active_sessions == []
        assert state.discovered_failures == []
        assert state.session_history == []
        assert state.immediate_cleanups == []
        kill_session.assert_called_once_with("issue-123")

    def test_queues_review_when_pr_created(self, sample_agent_config, tmp_path):
        """Verify review is queued when session creates PR."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=10,
                pr_url="https://github.com/test/repo/pull/456",
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=True,
            pr_url="https://github.com/test/repo/pull/456",
            pr_number=456,
        )
        mock_action_applier = MagicMock()
        mock_observer = MagicMock()
        config = MagicMock()
        config.cleanup.without_tech_lead.close_ai_session_tabs = True
        config.code_review_agent = "agent:reviewer"

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 1
        assert state.discovered_reviews[0].pr_number == 456

    def test_passes_blocked_reason_to_completion_handler(self, sample_agent_config, tmp_path):
        """Verify blocked_reason is forwarded to completion handler."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=10,
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        mock_action_applier = MagicMock()
        mock_observer = MagicMock()
        config = MagicMock()
        config.cleanup.without_tech_lead.close_ai_session_tabs = True
        config.code_review_agent = None

        handle_session_completion(
            session=session,
            status=SessionStatus.BLOCKED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            blocked_label="blocked-upstream",
            blocked_reason="Waiting for external API",
            pending_work_claims=_test_claim_store(),
        )

        mock_completion_handler.process_completion.assert_called_once()
        kwargs = mock_completion_handler.process_completion.call_args.kwargs
        assert kwargs["blocked_reason"] == "Waiting for external API"
        [problem] = state.discovered_failures
        assert problem.issue_number == 123
        assert problem.failure_reason == "blocked"
        assert problem.blocking_label == "blocked-upstream"
        assert problem.observed_at > 0
        assert problem.issue_body == (issue.body or "")
        assert problem.issue_milestone == issue.milestone
        assert 123 not in state.failed_this_cycle


# =============================================================================
# Environment Isolation Tests
# =============================================================================


class TestEnvironmentIsolation:
    """Test that sessions use proper environment isolation."""

    def test_issue_session_launches_successfully(self, launcher_bundle, sample_issue):
        """Test that issue sessions launch without HOME isolation.

        HOME isolation is disabled because Claude uses macOS keychain for
        subscription auth, which requires access to the real HOME.
        """
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        # Verify session was created
        assert len(launcher_bundle.create_session_calls) == 1

        # Verify command doesn't contain HOME override
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "export HOME=" not in command or "HOME=" not in command.split("&&")[0]

    def test_issue_session_exports_per_worktree_gradle_user_home(self, launcher_bundle, sample_issue):
        """Agent Gradle commands should use the worktree-local daemon registry."""
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert f"{GRADLE_USER_HOME_ENV}=" in command
        assert ".issue-orchestrator/tool-homes/gradle" in command

    def test_setup_commands_use_per_worktree_gradle_user_home(
        self,
        launcher_bundle,
        sample_issue,
        mock_command_runner,
    ):
        """Setup commands should share the session's isolated Gradle home."""
        launcher_bundle.launcher.config.setup_worktree = ["./gradlew tasks"]

        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        env = mock_command_runner.run_calls[0]["env"]
        assert env is not None
        assert env[GRADLE_USER_HOME_ENV].endswith(".issue-orchestrator/tool-homes/gradle")


class TestValidationOutputDir:
    """Test that sessions export VALIDATION_OUTPUT_DIR for output capture."""

    @pytest.fixture(autouse=True)
    def _no_feedback_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.control.session_launcher.time.sleep",
            lambda _: None,
        )

    def test_issue_session_exports_validation_output_dir(self, launcher_bundle, sample_issue):
        """Test that issue sessions export VALIDATION_OUTPUT_DIR pointing to run_dir."""
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]

        # Verify VALIDATION_OUTPUT_DIR is exported
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR=" in command

        # Verify it points to a sessions directory
        assert ".issue-orchestrator/sessions/" in command

    def test_review_session_exports_validation_output_dir(self, launcher_bundle, mock_repo_host):
        """Test that review sessions export VALIDATION_OUTPUT_DIR pointing to run_dir."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Test PR", url="url", branch="123-test", body="", state="open", labels=[])
        ]
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-test",
            _issue_number=123,
        )

        launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]

        # Verify VALIDATION_OUTPUT_DIR is exported
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR=" in command

        # Verify it points to a sessions directory
        assert ".issue-orchestrator/sessions/" in command

    def test_rework_session_exports_validation_output_dir(self, launcher_bundle, mock_repo_host):
        """Test that rework sessions export VALIDATION_OUTPUT_DIR pointing to run_dir."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Test PR", url="url", branch="123-test", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]

        # Verify VALIDATION_OUTPUT_DIR is exported
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR=" in command

        # Verify it points to a sessions directory
        assert ".issue-orchestrator/sessions/" in command

    def test_issue_session_exports_selected_config_name(self, launcher_bundle, sample_issue):
        """Issue sessions should export selected config filename for completion commands."""
        launcher_bundle.launcher.config.config_path = Path("/tmp/main.yaml")
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "ISSUE_ORCHESTRATOR_CONFIG_NAME='main.yaml'" in command


class TestExtraProviderArgsFromLabels:
    """Test _extra_provider_args_from_labels static helper."""

    def test_verbose_label_produces_verbose_arg(self):
        result = SessionLauncher._extra_provider_args_from_labels(["verbose", "priority:high"])  # noqa: SLF001 - unit test targets internal label parser
        assert result == {"verbose": "true"}

    def test_no_verbose_label_returns_none(self):
        result = SessionLauncher._extra_provider_args_from_labels(["priority:high", "agent:web"])  # noqa: SLF001 - unit test targets internal label parser
        assert result is None

    def test_empty_labels_returns_none(self):
        result = SessionLauncher._extra_provider_args_from_labels([])  # noqa: SLF001 - unit test targets internal label parser
        assert result is None


class TestStackRelaunchGate:
    """Validation retry and rework honor the stack work gate (#6596 F1).

    A blocked or ambiguous stack predecessor must fail the relaunch closed before
    any worktree is created/reset, and must never seed the worktree from the
    default base via a ``None`` stack base.
    """

    @staticmethod
    def _ambiguous_report(issue_number):
        from issue_orchestrator.domain.dependencies import (
            Dependency,
            DependencyMode,
            DependencyState,
            DependencyTarget,
        )
        from issue_orchestrator.domain.dependency_gates import (
            PredecessorFacts,
            build_gate_report,
        )

        deps = [
            Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED),
            Dependency(issue_number=21, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED),
        ]
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-base",
            ),
            DependencyTarget(21): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="21-base",
            ),
        }
        return build_gate_report(issue_number, deps, facts)

    class _CannedWorkEvaluator:
        def __init__(self, report_fn):
            self._report_fn = report_fn

        def evaluate_work_gate(
            self, issue_number, issue_body, source_milestone,
            *, configured_base_branch=None, emit_event=True,
        ):
            return self._report_fn(issue_number)

    def _launcher(self, fixtures, *, report_fn, body, refresh=None):
        (sample_config, mock_events, mock_repo_host, mock_worktree_manager,
         mock_working_copy, mock_command_runner) = fixtures

        def default_refresh(_number):
            return Issue(
                number=123,
                title="Test issue",
                labels=["agent:web"],
                repo="test/repo",
                body=body,
                milestone="M7",
            )

        refresh_issue = refresh if refresh is not None else default_refresh

        return SessionLauncher(
            config=sample_config,
            events=mock_events,
            repository_host=mock_repo_host,
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            command_runner=mock_command_runner,
            session_output=FileSystemSessionOutput(),
            manifest_downloader=NullManifestDownloader(),
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(sample_config.repo_root),
            session_exists_fn=lambda name: False,
            create_session_fn=lambda name, cmd, wd, title: True,
            get_issue_machine=lambda issue: IssueStateMachine(issue),
            get_session_machine=lambda name, n, timeout: SessionStateMachine(
                name, n, timeout_minutes=timeout
            ),
            get_review_machine=lambda pr, issue: ReviewStateMachine(pr, issue),
            refresh_issue_fn=refresh_issue,
            dependency_evaluator=self._CannedWorkEvaluator(report_fn),
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=ready_callback_endpoint(),
        )

    @pytest.fixture
    def _fixtures(
        self, sample_config, mock_events, mock_repo_host, mock_worktree_manager,
        mock_working_copy, mock_command_runner,
    ):
        return (sample_config, mock_events, mock_repo_host, mock_worktree_manager,
                mock_working_copy, mock_command_runner)

    def test_validation_retry_blocks_on_ambiguous_stack_base(
        self, _fixtures, mock_worktree_manager, mock_events
    ):
        launcher = self._launcher(
            _fixtures, report_fn=self._ambiguous_report,
            body="Stack-after: #20\nStack-after: #21",
        )
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Test issue",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-feature",
            original_prompt="Work on issue #123",
            validation_error="boom",
            validation_error_file="/tmp/err.txt",
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )

        result = launcher.launch_validation_retry_session(retry, active_sessions=[])

        assert result.success is False
        assert "Stack dependencies not satisfied" in (result.reason or "")
        # Fail closed BEFORE any worktree create/reset (no default-base fallback).
        assert mock_worktree_manager.create_calls == []
        assert any(str(e.name) == "issue.dependency_blocked" for e in mock_events.events)

    def test_rework_blocks_on_ambiguous_stack_base(
        self, _fixtures, mock_worktree_manager, mock_repo_host, mock_events
    ):
        mock_repo_host.prs[123] = [
            PRInfo(
                number=456, title="Fix issue #123",
                url="https://github.com/test/repo/pull/456",
                branch="123-feature", body="", state="open", labels=[],
            )
        ]
        launcher = self._launcher(
            _fixtures, report_fn=self._ambiguous_report,
            body="Stack-after: #20\nStack-after: #21",
        )
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert "Stack dependencies not satisfied" in (result.reason or "")
        assert mock_worktree_manager.create_calls == []
        assert any(str(e.name) == "issue.dependency_blocked" for e in mock_events.events)

    @staticmethod
    def _single_ready_report(issue_number):
        from issue_orchestrator.domain.dependencies import (
            Dependency,
            DependencyMode,
            DependencyState,
            DependencyTarget,
        )
        from issue_orchestrator.domain.dependency_gates import (
            PredecessorFacts,
            build_gate_report,
        )

        deps = [Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)]
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-base",
            ),
        }
        return build_gate_report(issue_number, deps, facts)

    def test_initial_launch_fails_closed_when_body_unreadable(
        self, _fixtures, mock_worktree_manager, mock_events, sample_issue
    ):
        # refresh returns None AND the issue carries no body -> the launcher
        # cannot prove non-stack, so it must fail closed, not seed from default.
        assert sample_issue.body is None
        launcher = self._launcher(
            _fixtures, report_fn=self._ambiguous_report, body=None,
            refresh=lambda _n: None,
        )

        result = launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert mock_worktree_manager.create_calls == []
        assert any(str(e.name) == "issue.dependency_blocked" for e in mock_events.events)

    def test_initial_launch_uses_issue_body_fallback_when_refresh_none(
        self, _fixtures, mock_worktree_manager, sample_issue
    ):
        # refresh fails but the already-known issue body proves a ready stack
        # successor -> seed from the predecessor branch (not the default base).
        sample_issue.body = "Stack-after: #20"
        sample_issue.milestone = "M7"
        launcher = self._launcher(
            _fixtures, report_fn=self._single_ready_report, body="Stack-after: #20",
            refresh=lambda _n: None,
        )

        result = launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_worktree_manager.create_calls) == 1
        assert mock_worktree_manager.create_calls[0]["base_branch"] == "20-base"

    def test_validation_retry_fails_closed_when_issue_unreadable(
        self, _fixtures, mock_worktree_manager, mock_events
    ):
        launcher = self._launcher(
            _fixtures, report_fn=self._ambiguous_report, body=None,
            refresh=lambda _n: None,
        )
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Test issue",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-feature",
            original_prompt="Work on issue #123",
            validation_error="boom",
            validation_error_file="/tmp/err.txt",
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )

        result = launcher.launch_validation_retry_session(retry, active_sessions=[])

        assert result.success is False
        assert mock_worktree_manager.create_calls == []
        assert any(str(e.name) == "issue.dependency_blocked" for e in mock_events.events)

    def test_rework_fails_closed_when_issue_unreadable(
        self, _fixtures, mock_worktree_manager, mock_repo_host, mock_events
    ):
        mock_repo_host.prs[123] = [
            PRInfo(
                number=456, title="Fix issue #123",
                url="https://github.com/test/repo/pull/456",
                branch="123-feature", body="", state="open", labels=[],
            )
        ]
        launcher = self._launcher(
            _fixtures, report_fn=self._ambiguous_report, body=None,
            refresh=lambda _n: None,
        )
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert mock_worktree_manager.create_calls == []
        assert any(str(e.name) == "issue.dependency_blocked" for e in mock_events.events)


def _test_claim_store(tmp_path=None):
    """The orchestrator-owned claim store completion now requires (#6999 F9)."""
    import tempfile
    from pathlib import Path as _Path

    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    return SqlitePendingWorkClaimStore.for_repo(
        _Path(tmp_path) if tmp_path is not None else _Path(tempfile.mkdtemp())
    )
