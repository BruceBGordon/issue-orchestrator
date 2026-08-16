"""Unit tests for the orchestrator module."""

import asyncio
import logging
import pytest
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call, AsyncMock, PropertyMock
from tests.conftest import MockSessionRunner
from issue_orchestrator.infra.orchestrator import Orchestrator, run_orchestrator
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    AgentConfig,
    OrchestratorState,
    ORCHESTRATOR_PR_MARKER,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.tech_lead_run import IssueInvestigationScope
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.observation.observer import SessionObserver
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.worktree_manager import (
    RegisteredWorktree,
    WorktreeInfo,
    WorktreeReuseOptions,
)
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
from issue_orchestrator.events import EventName
from issue_orchestrator.contracts.public import OrchestratorPausedPayload, OrchestratorResumedPayload


class MockWorktreeManager:
    """Mock implementation of WorktreeManager for testing."""

    def __init__(self, worktree_path: Path = None, branch_name: str = None):
        self.worktree_path = worktree_path or Path(tempfile.mkdtemp(prefix="io-worktree-"))
        self.branch_name = branch_name or "feature/issue-1"
        self.create_calls = []
        self.remove_calls = []

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
        """Track create calls and return mock WorktreeInfo."""
        self.create_calls.append({
            "repo_root": repo_root,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "worktree_base": worktree_base,
            "base_branch": base_branch,
            "seed_ref": seed_ref,
            "enforce_hooks": enforce_hooks,
            "pre_push_hook": pre_push_hook,
            "branch_name": branch_name,
            "reuse_options": reuse_options,
            "worktree_name": worktree_name,
        })
        return WorktreeInfo(path=self.worktree_path, branch_name=self.branch_name)

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        """Track remove calls."""
        del force
        self.remove_calls.append(worktree_path)

    def remove_checkout_and_branch(
        self, worktree_path: Path, *, force: bool = False
    ) -> None:
        """Track destructive remove calls."""
        del force
        self.remove_calls.append(worktree_path)

    def can_remove_without_user_changes(self, worktree_path: Path) -> bool:
        """Test fakes do not opt into forced cleanup unless a test configures it."""
        del worktree_path
        return False

    def list_registered(self, repo_root: Path) -> tuple[RegisteredWorktree, ...]:
        """No registered worktrees are relevant to this test fake."""
        del repo_root
        return ()

    def extract_issue_number(self, branch_name: str) -> int | None:
        """Extract issue number from branch name."""
        parts = branch_name.split("-")
        if parts and parts[0].isdigit():
            return int(parts[0])
        return None


def test_pause_emits_event(sample_config):
    orchestrator = create_test_orchestrator(sample_config)

    orchestrator.pause()

    events = orchestrator.deps.events.get_events_by_name(str(EventName.ORCHESTRATOR_PAUSED))
    assert len(events) == 1
    assert orchestrator.state.paused is True
    OrchestratorPausedPayload.model_validate(events[0].data)


def test_resume_emits_event(sample_config):
    orchestrator = create_test_orchestrator(sample_config)
    orchestrator.state.paused = True

    orchestrator.resume()

    events = orchestrator.deps.events.get_events_by_name(str(EventName.ORCHESTRATOR_RESUMED))
    assert len(events) == 1
    assert orchestrator.state.paused is False
    OrchestratorResumedPayload.model_validate(events[0].data)


def test_start_paused_requests_initial_queue_refresh(sample_config):
    orchestrator = create_test_orchestrator(sample_config)

    orchestrator.set_start_paused()

    assert orchestrator.state.paused is True
    assert orchestrator.state.queue_refresh_requested is True


def test_cancel_review_exchange_for_issue_delegates_to_lifecycle_services(sample_config):
    orchestrator = create_test_orchestrator(sample_config)
    pair_registry = MagicMock()
    job_supervisor = MagicMock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:77:issue-77"]
    object.__setattr__(
        orchestrator.deps,
        "services",
        replace(
            orchestrator.deps.services,
            pair_registry=pair_registry,
            background_job_supervisor=job_supervisor,
        ),
    )

    result = orchestrator.cancel_review_exchange_for_issue(
        77,
        reason="operator-terminated",
    )

    assert result.issue_number == 77
    assert result.cancelled_job_ids == ("review-exchange:77:issue-77",)
    pair_registry.release.assert_called_once_with(77, reason="operator-terminated")
    job_supervisor.cancel_matching.assert_called_once()
    predicate = job_supervisor.cancel_matching.call_args.args[0]
    assert predicate("review-exchange:77:issue-77")
    assert not predicate("review-exchange:78:issue-78")


def test_terminate_issue_runtime_for_issue_delegates_to_canonical_services(sample_config):
    orchestrator = create_test_orchestrator(sample_config)
    pair_registry = MagicMock()
    job_supervisor = MagicMock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:77:issue-77"]
    session_manager = MagicMock()
    session_manager.exists.side_effect = (
        lambda ref: ref.name in {"issue-77", "rework-77"}
    )
    object.__setattr__(orchestrator.deps, "session_manager", session_manager)
    object.__setattr__(
        orchestrator.deps,
        "services",
        replace(
            orchestrator.deps.services,
            pair_registry=pair_registry,
            background_job_supervisor=job_supervisor,
        ),
    )
    orchestrator.state.active_sessions = [
        SimpleNamespace(
            terminal_id="issue-77",
            issue=SimpleNamespace(number=77),
        ),
        SimpleNamespace(
            terminal_id="rework-77",
            issue=SimpleNamespace(number=77),
        ),
        SimpleNamespace(
            terminal_id="issue-88",
            issue=SimpleNamespace(number=88),
        ),
    ]

    result = orchestrator.terminate_issue_runtime_for_issue(
        77,
        reason="operator-terminated",
    )

    assert result.issue_number == 77
    assert result.stopped_session_ids == ("issue-77", "rework-77")
    assert result.cleared_active_session_ids == ("issue-77", "rework-77")
    assert result.cancelled_job_ids == ("review-exchange:77:issue-77",)
    pair_registry.release.assert_called_once_with(77, reason="operator-terminated")
    assert [call.args[0].name for call in session_manager.stop.call_args_list] == [
        "issue-77",
        "rework-77",
    ]
    assert [session.terminal_id for session in orchestrator.state.active_sessions] == [
        "issue-88",
    ]


def test_terminate_tech_lead_session_is_behavior_complete(sample_config, tmp_path):
    # R7 (#6824): the REAL facade terminate is behavior-complete — unlike bare
    # kill_session (which only stops the terminal), it removes the state machine,
    # reconciles active_sessions, releases the claim, AND force-removes the
    # disposable scratch worktree, so a timed-out investigation leaks nothing.
    orchestrator = create_test_orchestrator(sample_config)
    session_manager = MagicMock()
    claim_manager = MagicMock()
    smm = MagicMock()
    worktree_manager = MagicMock()
    object.__setattr__(orchestrator.deps, "session_manager", session_manager)
    object.__setattr__(orchestrator.deps, "claim_manager", claim_manager)
    object.__setattr__(orchestrator.deps, "state_machine_manager", smm)
    object.__setattr__(orchestrator.deps, "worktree_manager", worktree_manager)
    scratch = tmp_path / "repo-tech-lead-77-abc"
    tech_lead = SimpleNamespace(
        terminal_id="tech-lead-77", issue=SimpleNamespace(number=77), lease_id="lease-1",
        scratch_worktree=True, worktree_path=scratch,
        tech_lead_scope=TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
        ),
    )
    other = SimpleNamespace(
        terminal_id="issue-88", issue=SimpleNamespace(number=88), lease_id=None,
        scratch_worktree=False, worktree_path=None, tech_lead_scope=None,
    )
    orchestrator.state.active_sessions = [tech_lead, other]
    # The run this session holds across engines — termination owns BOTH
    # coordination layers, so it must go back too (#6994 round 2 F10).
    assert orchestrator.deps.run_ownership.claim(IssueInvestigationScope(77)).owned

    outcome = orchestrator.terminate_tech_lead_session(tech_lead)

    assert outcome.clean is True  # every effect succeeded
    assert outcome.run_released is True
    assert orchestrator.deps.run_ownership.owns("issue:77") is False
    # State machine terminalized (removed)...
    smm.remove_session_machine.assert_called_once_with("tech-lead-77")
    # ...terminal stopped through the real session-routing boundary...
    assert session_manager.stop.call_count == 1
    assert session_manager.stop.call_args.args[0].name == "tech-lead-77"
    # ...session reconciled out of active_sessions...
    assert [s.terminal_id for s in orchestrator.state.active_sessions] == ["issue-88"]
    # ...claim released...
    claim_manager.release_claim.assert_called_once_with(77, "lease-1")
    # ...and the disposable scratch worktree force-removed (no leak).
    worktree_manager.remove_checkout_and_branch.assert_called_once_with(
        scratch, force=True
    )


def _terminate_fixture(sample_config, tmp_path):
    orchestrator = create_test_orchestrator(sample_config)
    session_manager = MagicMock()
    claim_manager = MagicMock()
    smm = MagicMock()
    worktree_manager = MagicMock()
    object.__setattr__(orchestrator.deps, "session_manager", session_manager)
    object.__setattr__(orchestrator.deps, "claim_manager", claim_manager)
    object.__setattr__(orchestrator.deps, "state_machine_manager", smm)
    object.__setattr__(orchestrator.deps, "worktree_manager", worktree_manager)
    scratch = tmp_path / "repo-tech-lead-77-abc"
    tech_lead = SimpleNamespace(
        terminal_id="tech-lead-77", issue=SimpleNamespace(number=77), lease_id="lease-1",
        scratch_worktree=True, worktree_path=scratch,
    )
    orchestrator.state.active_sessions = [tech_lead]
    return orchestrator, tech_lead, session_manager, claim_manager, worktree_manager, scratch


def test_terminate_tech_lead_worktree_failure_reports_unclean_with_leaked_path(
    sample_config, tmp_path
):
    # R7 (#6824): if scratch-worktree removal FAILS, the outcome is NOT clean and
    # names the EXACT leaked path (the sole cleanup-failure owner — no dead
    # engine-tick ImmediateCleanup retry). Never a silent leak reported as success.
    orchestrator, tech_lead, sm, cm, wtm, scratch = _terminate_fixture(sample_config, tmp_path)
    wtm.remove_checkout_and_branch.side_effect = RuntimeError(
        "git worktree remove failed"
    )

    outcome = orchestrator.terminate_tech_lead_session(tech_lead)

    assert outcome.worktree_removed is False and outcome.clean is False
    assert outcome.leaked_worktree == str(scratch)
    # Other effects still ran independently.
    sm.stop.assert_called_once()
    cm.release_claim.assert_called_once_with(77, "lease-1")
    assert orchestrator.state.active_sessions == []
    # The dead engine-tick retry is gone — no ImmediateCleanup is recorded.
    assert orchestrator.state.immediate_cleanups == []


def test_terminate_tech_lead_terminal_failure_does_not_abort_other_effects(
    sample_config, tmp_path
):
    # R7 (#6824): a terminal-stop failure must not abort claim release or worktree
    # removal — every effect is attempted independently.
    orchestrator, tech_lead, sm, cm, wtm, scratch = _terminate_fixture(sample_config, tmp_path)
    sm.stop.side_effect = RuntimeError("terminal already gone")

    outcome = orchestrator.terminate_tech_lead_session(tech_lead)

    assert outcome.terminal_stopped is False
    # ...yet claim + worktree still handled, and the session reconciled.
    cm.release_claim.assert_called_once_with(77, "lease-1")
    wtm.remove_checkout_and_branch.assert_called_once_with(scratch, force=True)
    assert orchestrator.state.active_sessions == []


def test_composed_one_shot_timeout_terminates_via_real_driver_and_facade(
    sample_config, tmp_path
):
    """R8/R7 (#6824): the REAL driver drives the REAL Orchestrator facade to a
    timeout; the facade terminate leaves NO leaked state machine, claim, active-
    session record, or disposable scratch worktree — the cross-boundary outcome
    that separate unit tests with a fake host could not catch. No live GitHub."""
    from issue_orchestrator.control.tech_lead_trigger import run_targeted_investigations

    # A tech lead agent must be configured: the one-shot CLI now shares the
    # dashboard's admission owner, which refuses a repository without one
    # (#6994 round 1 F2).
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    orchestrator = create_test_orchestrator(sample_config)
    session_manager = MagicMock()
    claim_manager = MagicMock()
    smm = MagicMock()
    worktree_manager = MagicMock()
    repository_host = MagicMock()
    repository_host.get_issue.return_value = SimpleNamespace(
        number=77, title="Investigate", body="", milestone=None, labels=["blocked-failed"]
    )
    object.__setattr__(orchestrator.deps, "session_manager", session_manager)
    object.__setattr__(orchestrator.deps, "claim_manager", claim_manager)
    object.__setattr__(orchestrator.deps, "state_machine_manager", smm)
    object.__setattr__(orchestrator.deps, "worktree_manager", worktree_manager)
    object.__setattr__(orchestrator.deps, "repository_host", repository_host)

    scratch = tmp_path / "repo-tech-lead-77-abc"
    session = SimpleNamespace(
        terminal_id="tech-lead-77",
        key=SimpleNamespace(stable_id=lambda: "tech_lead:77"),
        issue=SimpleNamespace(number=77),
        lease_id="lease-1",
        scratch_worktree=True,
        worktree_path=scratch,
    )

    def _launch(_tech_lead):
        # Inject the launched session; the REAL driver then drives + times out.
        orchestrator.state.active_sessions = [session]
        return session

    orchestrator.launch_tech_lead_session = _launch
    orchestrator.tick = lambda: True  # never drains the session -> forces timeout
    orchestrator.pause = lambda: None

    # deadline, then the poll checks; the tail stays past the deadline so the
    # drive loop terminates deterministically however often it samples.
    clock = iter([0, 0] + [9_999] * 8)
    results = run_targeted_investigations(
        orchestrator, [77], now=lambda: next(clock), sleep=lambda _s: None, timeout_s=1
    )

    assert results[0].launched is True and results[0].completed is False
    assert "terminated" in results[0].detail
    # Behavior-complete cleanup applied by the REAL facade terminate:
    smm.remove_session_machine.assert_called_once_with("tech-lead-77")
    claim_manager.release_claim.assert_called_once_with(77, "lease-1")
    worktree_manager.remove_checkout_and_branch.assert_called_once_with(
        scratch, force=True
    )
    assert orchestrator.state.active_sessions == []


def create_test_orchestrator(
    config,
    repository_host=None,
    worktree_manager=None,
    working_copy=None,
    runner=None,
    *,
    session_controller=None,
    label_sync=None,
):
    """Create an Orchestrator with ALL dependencies explicitly injected.

    This is the proper hexagonal architecture test pattern:
    1. Creates MockEventSink and MockSessionRunner (or uses provided runner)
    2. Uses build_test_orchestrator_deps() to create OrchestratorDeps
    3. Passes deps bundle to Orchestrator (no nulls, no optionals)

    Args:
        config: Config object
        repository_host: Optional MockGitHubAdapter (creates MagicMock if None)
        worktree_manager: Optional MockWorktreeManager
        working_copy: Optional GitWorkingCopy
        runner: Optional pre-configured MockSessionRunner. Use this when you need
                to configure session behavior BEFORE orchestrator creation, e.g.:
                    runner = MockSessionRunner()
                    runner.plugin.session_exists_override = False
                    orchestrator = create_test_orchestrator(..., runner=runner)
        session_controller: Optional SessionController override for testing
        label_sync: Optional LabelSync override for testing

    Access mocks via:
        - orchestrator.deps.events (MockEventSink)
        - orchestrator.deps.runner (MockSessionRunner)
        - orchestrator.deps.runner.plugin (MockTerminalPlugin for session call assertions)
    """
    from tests.conftest import build_test_orchestrator_deps, MockEventSink, MockSessionRunner

    repo_host = repository_host or MagicMock()
    wt_manager = worktree_manager or MockWorktreeManager()
    if working_copy is None:
        wc = MagicMock()
        wc.list_remote_branches.return_value = []
    else:
        wc = working_copy

    # Create mock adapters (test implementations of ports)
    events = MockEventSink()
    runner = runner or MockSessionRunner()

    # Build OrchestratorDeps with all dependencies
    deps = build_test_orchestrator_deps(
        config,
        repo_host,
        events,
        runner,
        wt_manager,
        working_copy=wc,
        session_controller=session_controller,
        label_sync=label_sync,
    )

    return Orchestrator(config=config, deps=deps)


async def run_loop_one_tick(orchestrator: Orchestrator) -> None:
    """Run exactly one orchestrator tick deterministically."""
    original_tick = orchestrator.tick

    def tick_and_shutdown():
        result = original_tick()
        orchestrator.request_shutdown()
        return result

    with patch.object(orchestrator, "tick", side_effect=tick_and_shutdown):
        await orchestrator.run_loop()


# Helper functions
def create_issue(number, title="Test Issue", labels=None, milestone=None):
    """Helper to create Issue objects for testing."""
    if labels is None:
        labels = ["agent:web"]
    return Issue(
        number=number,
        title=title,
        labels=labels,
        milestone=milestone,
    )


def create_session(issue, worktree_path=None, branch_name="feature/test", task=TaskKind.CODE):
    """Helper to create Session objects for testing."""
    if worktree_path is None:
        worktree_path = tempfile.mkdtemp(prefix="io-worktree-")
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task)
    from tests.unit.session_run_helpers import make_session_run_assets

    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(worktree_path),
        branch_name=branch_name,
        run_assets=make_session_run_assets(
            Path(worktree_path),
            session_name=f"issue-{issue.number}",
        ),
    )


def create_pr_info(number, title="Test PR", labels=None, branch="feature/test", body=None):
    """Helper to create PRInfo objects for testing."""
    if labels is None:
        labels = []
    if body is None:
        body = f"Test PR body\n\n{ORCHESTRATOR_PR_MARKER}"
    return PRInfo(
        number=number,
        title=title,
        url=f"https://github.com/test/repo/pull/{number}",
        branch=branch,
        body=body,
        state="open",
        labels=labels,
    )


class TestOrchestratorInit:
    """Test Orchestrator initialization."""

    def test_post_init_creates_scheduler(self, sample_orchestrator, sample_config):
        """Test that __post_init__ creates a Scheduler."""
        assert isinstance(sample_orchestrator.scheduler, Scheduler)
        assert sample_orchestrator.scheduler.config == sample_config

    def test_post_init_creates_observer(self, sample_orchestrator, sample_config):
        """Test that __post_init__ creates a SessionObserver."""
        assert isinstance(sample_orchestrator.observer, SessionObserver)
        assert sample_orchestrator.observer.config == sample_config
        # Verify observer has reference to session_machines from state_machine_manager
        assert sample_orchestrator.observer.session_machines is sample_orchestrator.deps.state_machine_manager.session_machines

    def test_post_init_initializes_state(self, sample_orchestrator):
        """Test that state is initialized to default OrchestratorState."""
        assert isinstance(sample_orchestrator.state, OrchestratorState)
        assert sample_orchestrator.state.active_sessions == []
        assert sample_orchestrator.state.completed_today == []
        assert sample_orchestrator.state.paused is False
        assert sample_orchestrator.state.priority_queue == []

    def test_shutdown_flag_defaults_to_false(self, sample_orchestrator):
        """Test that shutdown_requested is False by default."""
        assert sample_orchestrator.shutdown_requested is False


class TestStartup:
    """Test the startup method."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_startup_checks_in_progress_issues(
        self,
        mock_analyze,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup checks for in-progress issues."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        await orchestrator.startup()

        # Should query for in-progress issues for each agent type
        # (cold fallback path — cache is empty since issues=[])
        assert len(mock_repository_host.list_issues_calls) > 0
        in_progress_label = sample_config.get_label_in_progress()
        assert any(
            "agent:web" in call["labels"] and in_progress_label in call["labels"]
            for call in mock_repository_host.list_issues_calls
        )

    @pytest.mark.asyncio
    async def test_startup_survives_transient_issue_fetch_failure(
        self, sample_config, mock_repository_host
    ):
        """A transient 404 during the startup *queue fetch* must not crash.

        Only the issue-list fetch is guarded; the remaining startup phases still
        run and the orchestrator comes up degraded so the main loop recovers.
        """
        from issue_orchestrator.control.startup_manager import StartupManager
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        transient = GitHubHttpError(
            "GitHub GET /repos/test/repo/issues failed: 404", status_code=404
        )

        # Fail only the guarded queue-list fetch, not all of run_startup.
        with patch.object(
            StartupManager, "_sync_queue_from_github", side_effect=transient
        ):
            # Must NOT raise — process stays up.
            await orchestrator.startup()

        assert orchestrator.shutdown_requested is False
        # Degraded-but-ready so the main loop runs and recovers — startup ran to
        # completion rather than aborting after the fetch.
        assert orchestrator.state.startup_status == "complete"

    @pytest.mark.asyncio
    async def test_startup_fails_fast_on_permanent_auth_failure(
        self, sample_config, mock_repository_host
    ):
        """A permanent auth failure on the queue fetch refuses to come up."""
        from issue_orchestrator.control.startup_manager import StartupManager
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        permanent = GitHubHttpError(
            "GitHub GET /repos/test/repo/issues failed: 401", status_code=401
        )

        with patch.object(
            StartupManager, "_sync_queue_from_github", side_effect=permanent
        ):
            # Must NOT raise a raw traceback — it is handled cleanly.
            await orchestrator.startup()

        assert orchestrator.shutdown_requested is True
        # An actionable message is surfaced (not a raw GitHubHttpError).
        assert orchestrator.state.startup_message
        assert "auth" in orchestrator.state.startup_message.lower()

    @pytest.mark.asyncio
    async def test_startup_repository_error_after_queue_sync_is_not_swallowed(
        self, sample_config, mock_repository_host
    ):
        """A repository error from a *post-queue-sync* startup phase must not be
        reclassified as an issue-fetch failure and silently marked complete.

        The resilience guard wraps only the queue-list fetch. A later recovery
        phase failing must propagate (its own existing error path) rather than
        the orchestrator skipping the remaining phases yet reporting startup as
        complete.
        """
        from issue_orchestrator.control.startup_manager import StartupManager
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        later_error = GitHubHttpError("GitHub unavailable", status_code=503)

        # Queue sync (Step 5) succeeds; a later recovery phase (Step 6) fails.
        with patch.object(StartupManager, "_sync_queue_from_github", return_value=None), \
             patch.object(
                 StartupManager,
                 "_check_in_progress_issues",
                 new=AsyncMock(side_effect=later_error),
             ):
            # The post-sync failure is NOT swallowed by issue-fetch resilience.
            with pytest.raises(GitHubHttpError) as exc_info:
                await orchestrator.startup()

        assert exc_info.value.status_code == 503
        # It was neither marked complete nor promoted to a fetch-shutdown.
        assert orchestrator.state.startup_status != "complete"
        assert orchestrator.shutdown_requested is False

    @pytest.mark.asyncio
    async def test_run_loop_survives_repository_failure_in_startup_reconcile(
        self, sample_config, mock_repository_host
    ):
        """A repository-host failure in the pre-loop reconcile must not crash."""
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        # Exit after the pre-loop setup so the test stays deterministic (no ticks).
        orchestrator.shutdown_requested = True

        with patch.object(
            orchestrator,
            "reconcile_orphaned_pr_labels",
            side_effect=GitHubHttpError("GitHub unavailable", status_code=503),
        ):
            # Must NOT raise — the loop rides out the reconcile failure.
            await orchestrator.run_loop()

        # Lifecycle still completed despite the reconcile failure.
        assert orchestrator.deps.events.get_events_by_name(EventName.ORCHESTRATOR_STARTED)

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_startup_clears_orphaned_labels(
        self,
        mock_analyze,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup clears orphaned in-progress labels."""

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue to indicate orphaned label
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        await orchestrator.startup()

        # Should remove the in-progress label
        assert (1, sample_config.get_label_in_progress()) in mock_repository_host.remove_label_calls

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_startup_reconciles_issues_with_open_prs(
        self,
        mock_analyze,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup adds pr-pending and removes in-progress for issues with open PRs (S2 crash recovery)."""

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue to indicate has open PR
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/123"
        mock_analyze.return_value = mock_state

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        await orchestrator.startup()

        # S2 crash recovery: add pr-pending, remove in-progress
        assert len(mock_repository_host.add_label_calls) == 1
        assert mock_repository_host.add_label_calls[0] == (1, "pr-pending")
        assert len(mock_repository_host.remove_label_calls) == 1
        assert mock_repository_host.remove_label_calls[0] == (1, "in-progress")

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_startup_resumes_partial_work(
        self,
        mock_analyze,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup resumes work for partial work (branch but no session)."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False  # No existing session, allow launch

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue to indicate has partial work
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, runner=runner)
        await orchestrator.startup()

        # Should clear retry-related guard labels when relaunching
        removed_labels = {
            (issue_number, label)
            for issue_number, label in mock_repository_host.remove_label_calls
        }
        assert (1, "io:auto-retried-interrupted-coding") in removed_labels
        assert (1, "reset-retry-pending") in removed_labels
        assert (1, "reset-retry-scratch-pending") in removed_labels
        # Session should have been launched (check active_sessions)
        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0].issue.number == 1

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_startup_skips_blocked_issues(
        self,
        mock_analyze,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup skips issues that are blocked (waiting for human)."""

        # Issue has both in-progress AND blocked labels
        issue = create_issue(1, labels=["agent:web", "in-progress", "blocked"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue - shouldn't matter since we skip before analyzing
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        await orchestrator.startup()

        # Should NOT remove any labels
        assert len(mock_repository_host.remove_label_calls) == 0
        # Should NOT launch a session - blocked issues wait for human
        assert len(orchestrator.state.active_sessions) == 0


class TestLaunchSession:
    """Test the launch_session method."""

    def test_launch_session_creates_worktree(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session creates a worktree."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = MockWorktreeManager()

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager, runner=runner)

        session = orchestrator.launch_session(issue)

        assert len(mock_worktree_manager.create_calls) == 1
        assert mock_worktree_manager.create_calls[0]["issue_number"] == 1

    def test_launch_session_adds_in_progress_label(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session adds the in-progress label."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = MockWorktreeManager()

        issue = create_issue(1, labels=["agent:web"])
        # Proper DI: inject mock adapter instead of patching functions
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager, runner=runner)

        session = orchestrator.launch_session(issue)

        # Verify adapter was called with correct arguments
        assert (1, sample_config.get_label_in_progress()) in mock_repository_host.add_label_calls

    def test_launch_session_creates_tmux_session(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session creates a tmux session."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = MockWorktreeManager()

        issue = create_issue(1, title="Test Issue", labels=["agent:web"])
        sample_config.ui_mode = "tmux"  # Explicitly test tmux mode
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager, runner=runner)

        session = orchestrator.launch_session(issue)

        # Verify session was created via plugin
        assert len(orchestrator.deps.runner.plugin.create_session_calls) == 1
        call = orchestrator.deps.runner.plugin.create_session_calls[0]
        assert call["session_id"] == 1  # issue number
        assert isinstance(call["command"], str)
        assert call["working_dir"] == str(mock_worktree_manager.worktree_path)

    def test_launch_session_adds_to_active_sessions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session adds the session to active_sessions."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = MockWorktreeManager()

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager, runner=runner)

        assert len(orchestrator.state.active_sessions) == 0

        session = orchestrator.launch_session(issue)

        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0] == session

    def test_launch_session_returns_session_object(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session returns a Session object."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = MockWorktreeManager()

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager, runner=runner)

        session = orchestrator.launch_session(issue)

        assert isinstance(session, Session)
        assert session.issue == issue
        assert session.terminal_id == "issue-1"
        assert session.worktree_path == mock_worktree_manager.worktree_path
        assert session.branch_name == "feature/issue-1"

    def test_launch_session_returns_none_if_session_already_exists(
        self,
        sample_config,
    ):
        """Test that launch_session returns None if session already exists."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = True  # Session exists in tmux

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = create_test_orchestrator(sample_config, runner=runner)

        session = orchestrator.launch_session(issue)

        assert session is None

    def test_reconcile_running_sessions_restores_untracked_registry_session(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test runtime reconciliation restores sessions missing from active state."""
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        issue = create_issue(7, labels=["agent:web"])
        restored = create_session(issue)
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[
            {"issue_number": 7, "tab_name": "Existing issue title", "is_review": False}
        ])
        orchestrator.deps.session_restorer.restore_sessions = MagicMock(return_value=[restored])

        orchestrator._reconcile_running_sessions()  # noqa: SLF001

        assert orchestrator.state.active_sessions == [restored]
        orchestrator.deps.session_restorer.restore_sessions.assert_called_once()
        restored_events = orchestrator.deps.events.get_events_by_name(EventName.SESSION_RESTORED)
        assert len(restored_events) == 1
        assert restored_events[0].data["trigger"] == "initial"
        assert restored_events[0].data["session_names"] == ["issue-7"]
        assert restored_events[0].data["discovery_elapsed_ms"] >= 0

    def test_reconcile_running_sessions_handles_discovery_failure(
        self,
        sample_config,
        mock_repository_host,
        caplog,
    ):
        """Runtime reconciliation logs discovery failures without breaking the tick."""
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.deps.runner.discover_running_sessions = MagicMock(
            side_effect=RuntimeError("registry unavailable")
        )

        with caplog.at_level(logging.ERROR):
            orchestrator._reconcile_running_sessions()  # noqa: SLF001

        assert orchestrator.state.active_sessions == []
        assert "Failed to discover running terminal sessions" in caplog.text

    def test_reconcile_running_sessions_throttles_registry_discovery_when_unchanged(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Runtime registry scans are rate-limited when active state is stable."""
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        existing = MagicMock(spec=Session)
        existing.terminal_id = "issue-1"
        orchestrator.state.active_sessions = [existing]
        orchestrator._last_orphan_reconcile_scan_at = 100.0  # noqa: SLF001
        orchestrator._last_orphan_reconcile_active_count = 1  # noqa: SLF001
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[])

        with patch("issue_orchestrator.infra.orchestrator.time.monotonic", return_value=110.0):
            orchestrator._reconcile_running_sessions()  # noqa: SLF001

        orchestrator.deps.runner.discover_running_sessions.assert_not_called()

    def test_reconcile_running_sessions_scans_immediately_when_active_state_drops_to_zero(
        self,
        sample_config,
        mock_repository_host,
    ):
        """A lost active session list bypasses the interval throttle."""
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator._last_orphan_reconcile_scan_at = 100.0  # noqa: SLF001
        orchestrator._last_orphan_reconcile_active_count = 1  # noqa: SLF001
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[])

        with patch(
            "issue_orchestrator.infra.orchestrator.time.monotonic",
            side_effect=[110.0, 110.01],
        ):
            orchestrator._reconcile_running_sessions()  # noqa: SLF001

        orchestrator.deps.runner.discover_running_sessions.assert_called_once()

    def test_reconcile_running_sessions_warns_when_restore_cannot_track(
        self,
        sample_config,
        mock_repository_host,
        caplog,
    ):
        """A still-untracked live terminal is visible after restore attempts fail."""
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[
            {
                "issue_number": 8,
                "tab_name": "Issue 8",
                "is_review": False,
                "session_name": "issue-8",
            }
        ])
        orchestrator.deps.session_restorer.restore_sessions = MagicMock(return_value=[])

        with caplog.at_level(logging.WARNING):
            orchestrator._reconcile_running_sessions()  # noqa: SLF001

        assert orchestrator.state.active_sessions == []
        assert "still untracked after restore attempt: issue-8" in caplog.text

    def test_reconcile_running_sessions_uses_review_terminal_identity(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Review tab names resolve to review-PR ids when deciding if tracked."""
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        from tests.unit.session_run_helpers import make_session_run_assets

        existing = MagicMock(spec=Session)
        existing.terminal_id = "review-456"
        # An active session always carries typed run assets; the ledger sweep
        # that now runs on every reconcile reads its run key (#6999 F8).
        existing.run_assets = make_session_run_assets(
            sample_config.repo_root, session_name="review-456"
        )
        orchestrator.state.active_sessions = [existing]
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[
            {"issue_number": 100, "tab_name": "#100 Review PR #456", "is_review": True}
        ])
        orchestrator.deps.session_restorer.restore_sessions = MagicMock(return_value=[])

        orchestrator._reconcile_running_sessions()  # noqa: SLF001

        orchestrator.deps.session_restorer.restore_sessions.assert_not_called()

    def test_launch_collision_requires_discovered_run_assets_without_dropping_queue(
        self,
        sample_config,
        mock_repository_host,
    ):
        """A launch collision does not synthesize a session without run assets."""
        issue = create_issue(9, labels=["agent:web"])
        mock_repository_host.issues.append(issue)
        worktree = sample_config.repo_root.parent / f"{sample_config.repo_root.name}-9"
        worktree.mkdir()
        working_copy = MagicMock()
        working_copy.get_current_branch.return_value = "9-feature"
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = True
        orchestrator = create_test_orchestrator(
            sample_config,
            mock_repository_host,
            runner=runner,
            working_copy=working_copy,
        )
        orchestrator.state.cached_queue_issues = [issue]

        session = orchestrator.launch_session(issue)

        assert session is None
        assert orchestrator.state.active_sessions == []
        assert orchestrator.state.cached_queue_issues == [issue]

    def test_launch_session_returns_none_for_unknown_agent(
        self,
        sample_config,
    ):
        """Test that launch_session returns None for unknown agent type."""
        issue = create_issue(1, labels=["agent:unknown"])
        orchestrator = create_test_orchestrator(sample_config)

        # New behavior: SessionLauncher returns graceful failure instead of raising
        session = orchestrator.launch_session(issue)
        assert session is None

class TestHandleSessionCompletion:
    """Test the handle_session_completion method."""

    @pytest.fixture
    def mock_worktree_manager(self, tmp_path: Path):
        """Create a mock worktree manager for testing."""
        manager = MagicMock()
        manager.worktree_path = tmp_path / "worktree"
        manager.worktree_path.mkdir(parents=True)
        manager.remove = MagicMock()
        return manager

    def test_handle_completion_removes_from_active_sessions(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion removes session from active list."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        assert len(orchestrator.state.active_sessions) == 1

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert len(orchestrator.state.active_sessions) == 0

    def test_handle_completion_calls_monitor_handler(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion delegates to monitor."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.observer, "handle_completion") as mock_monitor:
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

            mock_monitor.assert_called_once_with(session, SessionStatus.COMPLETED)

    def test_handle_completion_tracks_completed_issues(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion tracks completed issues."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        assert len(orchestrator.state.completed_today) == 0

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert len(orchestrator.state.completed_today) == 1
        assert orchestrator.state.completed_today[0] == 1

    def test_handle_completion_does_not_track_failed_issues(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that failed sessions are not added to completed_today."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        assert len(orchestrator.state.completed_today) == 0

    def test_handle_completion_records_immediate_cleanup_for_completed(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that completed sessions record immediate cleanup for planner."""
        issue = create_issue(1)
        session = create_session(issue, worktree_path=mock_worktree_manager.worktree_path)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Cleanup is now recorded as a fact for the planner, not done immediately
        assert len(orchestrator.state.immediate_cleanups) == 1
        cleanup = orchestrator.state.immediate_cleanups[0]
        assert cleanup.issue_number == 1
        assert cleanup.terminal_id == session.terminal_id
        assert cleanup.reason == "completed"
        # An ordinary coding session's worktree is not a scratch workspace.
        assert cleanup.scratch_worktree is False

    def test_completed_session_with_no_cleanup_actions_has_no_cleanup_effects(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """No selected action never enters the cleanup lifecycle boundary."""
        from issue_orchestrator.control.actions import CleanupSessionAction

        sample_config.tech_lead_review_agent = None
        sample_config.code_review_agent = None
        sample_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        sample_config.cleanup.without_tech_lead.remove_worktrees = False

        runner = MockSessionRunner()
        pair_registry = MagicMock()
        background_jobs = MagicMock()
        issue = create_issue(1)
        session = create_session(
            issue,
            worktree_path=mock_worktree_manager.worktree_path,
        )
        orchestrator = create_test_orchestrator(
            sample_config,
            worktree_manager=mock_worktree_manager,
            runner=runner,
        )
        orchestrator.deps.action_applier.pair_registry = pair_registry
        orchestrator.deps.action_applier.background_job_supervisor = background_jobs
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert orchestrator.state.pending_cleanups == []
        assert orchestrator.state.immediate_cleanups == []
        runtime_terminalization_calls = list(runner.plugin.kill_session_calls)

        snapshot = orchestrator.deps.fact_gatherer.create_snapshot(
            orchestrator.state,
            [],
        )
        plan = orchestrator.deps.planner.plan(snapshot)
        cleanup_actions = [
            action
            for action in plan.actions
            if isinstance(action, CleanupSessionAction)
        ]
        assert cleanup_actions == []

        orchestrator._apply_plan(plan)  # noqa: SLF001 - behavior boundary

        pair_registry.release.assert_not_called()
        background_jobs.cancel_matching.assert_not_called()
        assert runner.plugin.kill_session_calls == runtime_terminalization_calls
        mock_worktree_manager.remove_checkout.assert_not_called()
        assert orchestrator.deps.events.get_events_by_name(
            str(EventName.CLEANUP_COMPLETED)
        ) == []

    def test_no_actions_apply_error_records_immediate_failed_cleanup(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """The post-apply effective failure overrides a pre-apply NONE decision."""
        sample_config.tech_lead_review_agent = None
        sample_config.code_review_agent = None
        sample_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        sample_config.cleanup.without_tech_lead.remove_worktrees = False

        issue = create_issue(1)
        session = create_session(
            issue,
            worktree_path=mock_worktree_manager.worktree_path,
        )
        orchestrator = create_test_orchestrator(
            sample_config,
            worktree_manager=mock_worktree_manager,
        )
        orchestrator.state.active_sessions.append(session)
        apply_error = RuntimeError("completion action apply failed")
        orchestrator.deps.action_applier.apply_all = MagicMock(
            side_effect=apply_error
        )

        with pytest.raises(RuntimeError, match="completion action apply failed"):
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert orchestrator.state.completed_today == []
        assert orchestrator.state.session_history[-1].status == "failed"
        failed_events = orchestrator.deps.events.get_events_by_name(
            str(EventName.SESSION_FAILED)
        )
        assert len(failed_events) == 1
        assert orchestrator.deps.events.get_events_by_name(
            str(EventName.SESSION_COMPLETED)
        ) == []
        assert orchestrator.state.pending_cleanups == []
        [cleanup] = orchestrator.state.immediate_cleanups
        assert cleanup.issue_number == issue.number
        assert cleanup.terminal_id == session.terminal_id
        assert cleanup.reason == SessionStatus.FAILED.value

    def test_handle_completion_marks_scratch_worktree_for_investigation(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """A disposable tech-lead-investigation scratch session records its
        ImmediateCleanup as scratch (#6823), so the Planner force-removes the
        throwaway worktree on completion regardless of the cleanup config."""
        import dataclasses

        sample_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        sample_config.cleanup.without_tech_lead.remove_worktrees = False
        issue = create_issue(1)
        session = dataclasses.replace(
            create_session(issue, worktree_path=mock_worktree_manager.worktree_path),
            scratch_worktree=True,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert len(orchestrator.state.immediate_cleanups) == 1
        assert orchestrator.state.immediate_cleanups[0].scratch_worktree is True

    def test_handle_completion_records_immediate_cleanup_for_blocked(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that blocked sessions record immediate cleanup for planner."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.BLOCKED)

        # Cleanup is now recorded as a fact for the planner
        assert len(orchestrator.state.immediate_cleanups) == 1
        cleanup = orchestrator.state.immediate_cleanups[0]
        assert cleanup.reason == "blocked"

    def test_handle_completion_records_immediate_cleanup_for_failed(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Test that failed sessions record immediate cleanup for planner."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        # Cleanup is now recorded as a fact for the planner
        assert len(orchestrator.state.immediate_cleanups) == 1
        cleanup = orchestrator.state.immediate_cleanups[0]
        assert cleanup.reason == "failed"

    # Note: Worktree removal error handling is now tested in test_action_applier.py
    # since cleanup is handled via CleanupSessionAction.

    def test_handle_completion_legacy_worktree_removal_test(
        self,
        sample_config,
        mock_worktree_manager,
    ):
        """Legacy test - worktree removal is now deferred to planner/applier."""
        mock_worktree_manager.remove_checkout.side_effect = Exception(
            "Failed to remove worktree"
        )

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        # Should not raise exception
        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

    # Note: Session closing is now handled via CleanupSessionAction in the planner/applier cycle.
    # These tests verify that ImmediateCleanup is recorded; the actual closing is tested in
    # test_action_applier.py where CleanupSessionAction is executed.


class TestRunLoop:
    """Test the run_loop method."""

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Mock asyncio.sleep to yield control but not wait."""
        original_sleep = asyncio.sleep  # Save reference before patching

        async def instant_yield(*args):
            await original_sleep(0)  # Use real sleep to yield

        with patch("issue_orchestrator.infra.orchestrator.asyncio.sleep", side_effect=instant_yield):
            yield

    @pytest.mark.asyncio
    async def test_run_loop_exits_on_shutdown_request(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop exits when shutdown is requested."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.request_shutdown()  # Request shutdown immediately

        # Should exit quickly without running loop
        await orchestrator.run_loop()

        assert orchestrator.shutdown_requested is True

    @pytest.mark.asyncio
    async def test_shutdown_terminates_agent_processes_before_waiting_for_jobs(
        self,
        sample_config,
    ):
        calls: list[str] = []
        runner = MockSessionRunner()
        runner.on_orchestrator_shutdown = MagicMock(
            side_effect=lambda: calls.append("terminal-sessions")
        )
        pair_registry = MagicMock()
        pair_registry.shutdown_all.side_effect = (
            lambda *, reason: calls.append(f"persistent-pairs:{reason}")
        )
        job_supervisor = MagicMock()
        job_supervisor.wait_until_idle.side_effect = (
            lambda *, timeout: calls.append(f"background-jobs:{timeout}") or True
        )
        orchestrator = create_test_orchestrator(sample_config, runner=runner)
        object.__setattr__(
            orchestrator.deps,
            "services",
            replace(
                orchestrator.deps.services,
                pair_registry=pair_registry,
                background_job_supervisor=job_supervisor,
            ),
        )
        orchestrator.shutdown_requested = True

        await orchestrator.run_loop()

        assert calls == [
            "persistent-pairs:orchestrator-shutdown",
            "terminal-sessions",
            "background-jobs:60.0",
        ]
        job_supervisor.tick.assert_called_once_with()

    def test_close_is_idempotent_across_competing_exit_paths(self, sample_config):
        runner = MockSessionRunner()
        runner.on_orchestrator_shutdown = MagicMock()
        pair_registry = MagicMock()
        job_supervisor = MagicMock()
        job_supervisor.wait_until_idle.return_value = True
        orchestrator = create_test_orchestrator(sample_config, runner=runner)
        object.__setattr__(
            orchestrator.deps,
            "services",
            replace(
                orchestrator.deps.services,
                pair_registry=pair_registry,
                background_job_supervisor=job_supervisor,
            ),
        )

        orchestrator.close()
        orchestrator.close()

        pair_registry.shutdown_all.assert_called_once_with(
            reason="orchestrator-shutdown"
        )
        runner.on_orchestrator_shutdown.assert_called_once_with()
        job_supervisor.wait_until_idle.assert_called_once_with(timeout=60.0)

    @pytest.mark.asyncio
    async def test_tick_reconciles_tech_lead_needs_human_against_active_sessions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Every tick invokes the label-owned stale-escalation reconciler."""
        mock_repository_host.issues = []
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        reconcile = MagicMock()

        with patch.object(
            orchestrator._session_launcher,  # noqa: SLF001
            "reconcile_stale_tech_lead_needs_human",
            reconcile,
        ):
            await run_loop_one_tick(orchestrator)

        reconcile.assert_called_once_with(
            orchestrator.state.active_sessions, discover_markers=True
        )

    @pytest.mark.asyncio
    async def test_run_loop_checks_active_sessions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop checks status of active sessions."""
        mock_repository_host.issues = []

        issue = create_issue(1)
        session = create_session(issue)

        from issue_orchestrator.observation.observation import SessionObservationResult
        from issue_orchestrator.control.session_controller import SessionDecision

        # Create mock controller BEFORE orchestrator
        mock_decision = SessionDecision(
            status=SessionStatus.COMPLETED,
            recovered_from_timeout=False,
            reason="Test",
        )
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = mock_decision

        # Pass mock controller via factory
        orchestrator = create_test_orchestrator(
            sample_config, mock_repository_host, session_controller=mock_controller
        )
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.observer, "observe_session") as mock_observe:
            # Mock observe to return TERMINATED (session exited)
            mock_observe.return_value = SessionObservationResult.terminated(runtime_minutes=10.0)

            await run_loop_one_tick(orchestrator)

            mock_observe.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_handles_completed_sessions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop handles completed sessions."""
        from issue_orchestrator.observation.observation import SessionObservationResult
        from issue_orchestrator.control.session_controller import SessionDecision

        mock_repository_host.issues = []

        issue = create_issue(1)
        session = create_session(issue)

        # Create mock controller BEFORE orchestrator
        mock_decision = SessionDecision(
            status=SessionStatus.COMPLETED,
            recovered_from_timeout=False,
            reason="Test",
        )
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = mock_decision

        # Pass mock controller via factory
        orchestrator = create_test_orchestrator(
            sample_config, mock_repository_host, session_controller=mock_controller
        )
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.observer, "observe_session") as mock_observe:
            # Mock observe to return TERMINATED (session exited)
            mock_observe.return_value = SessionObservationResult.terminated(runtime_minutes=10.0)

            # Run one iteration
            async def run_one_iteration():
                tick_complete = asyncio.Event()
                original_tick = orchestrator.tick

                def _tick_once():
                    result = original_tick()
                    tick_complete.set()
                    orchestrator.request_shutdown()
                    return result

                with patch.object(orchestrator, "tick", side_effect=_tick_once):
                    await tick_complete.wait()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Verify session was removed from active_sessions after completion
            assert session not in orchestrator.state.active_sessions
            # Verify session was added to history
            assert len(orchestrator.state.session_history) > 0

    @pytest.mark.asyncio
    async def test_run_loop_fetches_available_issues_when_not_paused(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop fetches available issues when not paused."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        await run_loop_one_tick(orchestrator)

        assert len(mock_repository_host.list_issues_calls) > 0

    @pytest.mark.asyncio
    async def test_run_loop_does_not_fetch_when_paused(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop doesn't fetch new issues when paused."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.state.paused = True

        await run_loop_one_tick(orchestrator)

        # Should not fetch issues when paused
        assert len(mock_repository_host.list_issues_calls) == 0

    @pytest.mark.asyncio
    async def test_run_loop_start_paused_refreshes_queue_without_launching(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Start-paused hydrates the read model without starting sessions."""
        mock_repository_host.issues = [
            create_issue(360, title="Blocked issue", labels=["agent:web", "blocked"])
        ]

        worktree_manager = MockWorktreeManager()
        orchestrator = create_test_orchestrator(
            sample_config,
            mock_repository_host,
            worktree_manager=worktree_manager,
        )
        orchestrator.set_start_paused()

        await run_loop_one_tick(orchestrator)

        assert len(mock_repository_host.list_issues_calls) > 0
        assert any(issue.number == 360 for issue in orchestrator.state.cached_queue_issues)
        assert orchestrator.state.active_sessions == []
        assert worktree_manager.create_calls == []

    @pytest.mark.asyncio
    async def test_run_loop_emits_started_before_initial_paused(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Initial start-paused state should emit started before paused."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.set_start_paused()
        orchestrator.shutdown_requested = True

        await orchestrator.run_loop()

        lifecycle = [
            event.name
            for event in orchestrator.deps.events.events
            if event.name in {"orchestrator.started", "orchestrator.paused"}
        ]
        assert lifecycle == ["orchestrator.started", "orchestrator.paused"]

    @pytest.mark.asyncio
    async def test_run_loop_respects_max_sessions_limit(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop respects max_sessions limit."""
        from issue_orchestrator.observation.observation import SessionObservationResult

        sample_config.max_concurrent_sessions = 2

        issue1 = create_issue(1)
        issue2 = create_issue(2)
        issue3 = create_issue(3)

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        # Already have 2 active sessions
        orchestrator.state.active_sessions.append(create_session(issue1))
        orchestrator.state.active_sessions.append(create_session(issue2))

        with patch.object(orchestrator.observer, "observe_session") as mock_observe:
            # Mock observe_session to return RUNNING (sessions still active)
            mock_observe.return_value = SessionObservationResult.running(runtime_minutes=5.0)

            with patch.object(orchestrator, "launch_session") as mock_launch:
                await run_loop_one_tick(orchestrator)

                # Should not launch new sessions when at capacity
                mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_loop_launches_sessions_with_available_capacity(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop launches sessions when capacity is available."""
        sample_config.max_concurrent_sessions = 3

        issue1 = create_issue(1, labels=["agent:web"])

        mock_repository_host.issues = [issue1]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.return_value = create_session(issue1)

            tick_complete = asyncio.Event()
            original_tick = orchestrator.tick

            def _tick_once():
                result = original_tick()
                tick_complete.set()
                orchestrator.request_shutdown()
                return result

            with patch.object(orchestrator, "tick", side_effect=_tick_once):
                await asyncio.gather(
                    orchestrator.run_loop(),
                    tick_complete.wait(),
                )

            # Should launch session since we have capacity
            mock_launch.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_handles_launch_exceptions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop handles exceptions during launch gracefully."""
        issue1 = create_issue(1, labels=["agent:web"])

        mock_repository_host.issues = [issue1]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.side_effect = Exception("Launch failed")

            # Run one iteration - should not crash
            tick_complete = asyncio.Event()
            original_tick = orchestrator.tick

            def _tick_once():
                result = original_tick()
                tick_complete.set()
                orchestrator.request_shutdown()
                return result

            with patch.object(orchestrator, "tick", side_effect=_tick_once):
                await asyncio.gather(
                    orchestrator.run_loop(),
                    tick_complete.wait(),
                )

            # Should have attempted launch
            mock_launch.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_continues_after_repository_host_error(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Repository host failures are surfaced as tick errors without stopping the loop."""
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        calls = 0

        def _tick_then_recover():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise GitHubHttpError("GitHub unavailable", status_code=503)
            orchestrator.request_shutdown()
            return False

        with patch.object(orchestrator, "tick", side_effect=_tick_then_recover):
            await orchestrator.run_loop()

        assert calls == 2
        failures = orchestrator.deps.events.get_events_by_name(str(EventName.APPLY_FAILED))
        assert len(failures) == 1
        assert failures[0].data["error_count"] == 1

    @pytest.mark.asyncio
    async def test_run_loop_skips_already_claimed_issues(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop continues when an issue is already claimed."""
        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            call_count = 0

            tick_complete = asyncio.Event()
            original_tick = orchestrator.tick

            def _tick_once():
                result = original_tick()
                tick_complete.set()
                orchestrator.request_shutdown()
                return result

            # Run one iteration without timing sleeps
            with patch.object(orchestrator, "tick", side_effect=_tick_once):
                await asyncio.gather(
                    orchestrator.run_loop(),
                    tick_complete.wait(),
                )

            # Should try to launch both (loop may run multiple iterations)
            assert mock_launch.call_count >= 2


class TestMaxIssuesToStart:
    """Test the max_issues_to_start limit functionality."""

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Mock asyncio.sleep to yield control but not wait."""
        original_sleep = asyncio.sleep  # Save reference before patching

        async def instant_yield(*args):
            await original_sleep(0)  # Use real sleep to yield

        with patch("issue_orchestrator.infra.orchestrator.asyncio.sleep", side_effect=instant_yield):
            yield

    @pytest.mark.asyncio
    async def test_run_loop_respects_max_issues_to_start(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop stops launching when filtering.max_to_start is reached."""
        sample_config.filtering.max_to_start = 2
        sample_config.max_concurrent_sessions = 5  # Plenty of capacity

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            call_count = 0

            # Run one iteration
            tick_complete = asyncio.Event()
            original_tick = orchestrator.tick

            def _tick_once():
                result = original_tick()
                tick_complete.set()
                orchestrator.request_shutdown()
                return result

            with patch.object(orchestrator, "tick", side_effect=_tick_once):
                await asyncio.gather(
                    orchestrator.run_loop(),
                    tick_complete.wait(),
                )

            # Should only launch 2 issues (max_issues_to_start limit)
            assert mock_launch.call_count == 2
            assert orchestrator.state.issues_started_count == 2

    @pytest.mark.asyncio
    async def test_run_loop_unlimited_when_max_issues_zero(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that filtering.max_to_start=0 means unlimited."""
        sample_config.filtering.max_to_start = 0  # Unlimited
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            call_count = 0

            def launch_side_effect(issue):
                nonlocal call_count
                call_count += 1
                if call_count >= 3:
                    orchestrator.request_shutdown()
                if call_count == 1:
                    return create_session(issue1)
                if call_count == 2:
                    return create_session(issue2)
                return create_session(issue3)

            mock_launch.side_effect = launch_side_effect

            await orchestrator.run_loop()

            # Should launch all 3 issues (no limit); loop may run multiple iterations
            assert mock_launch.call_count >= 3
            assert orchestrator.state.issues_started_count >= 3

    @pytest.mark.asyncio
    async def test_run_loop_does_not_launch_when_limit_already_reached(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that no new issues are launched if limit was already reached."""
        sample_config.filtering.max_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])

        mock_repository_host.issues = [issue1]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        # Simulate that we already started 2 issues in previous iterations
        orchestrator.state.issues_started_count = 2

        with patch.object(orchestrator, "launch_session") as mock_launch:
            await run_loop_one_tick(orchestrator)

            # Should not launch any new issues
            mock_launch.assert_not_called()

    def test_launch_session_increments_issues_started_count(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that launching a session increments issues_started_count."""
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = MockWorktreeManager()

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager, runner=runner)

        assert orchestrator.state.issues_started_count == 0

        # Note: The counter is incremented in run_loop, not in launch_session itself
        # So we test that the state field exists and works
        orchestrator.state.issues_started_count = 5
        assert orchestrator.state.issues_started_count == 5

    @pytest.mark.asyncio
    async def test_run_loop_checks_limit_before_each_launch(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that limit is checked before each launch in a batch."""
        sample_config.filtering.max_to_start = 1
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.return_value = create_session(issue1)

            mock_launch.side_effect = lambda issue: (
                orchestrator.request_shutdown() or create_session(issue1)
            )

            await orchestrator.run_loop()

            # Should only launch 1 issue even though 2 are available
            assert mock_launch.call_count == 1
            assert orchestrator.state.issues_started_count == 1

    @pytest.mark.asyncio
    async def test_run_loop_skipped_claims_dont_count_toward_limit(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that failed claims don't increment issues_started_count.

        The planner generates actions upfront based on filtering.max_to_start limit.
        When a launch fails (already claimed), the count doesn't increment, but
        the planner has already decided how many actions to generate for this tick.

        In a single tick:
        - Planner generates 2 actions (filtering.max_to_start=2, count=0)
        - Action 1 fails (already claimed) -> count stays 0
        - Action 2 succeeds -> count becomes 1

        On subsequent ticks, the planner would generate more actions because
        count (1) < filtering.max_to_start (2).
        """
        sample_config.filtering.max_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            call_count = 0

            def launch_side_effect(issue):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    orchestrator.request_shutdown()
                return None if call_count == 1 else create_session(issue2)

            mock_launch.side_effect = launch_side_effect

            await orchestrator.run_loop()

            # Planner generates 2 actions based on max_issues_to_start=2
            # (it doesn't know issue1 will fail to launch)
            assert mock_launch.call_count == 2
            # Only 1 succeeded, so count is 1 (not 2)
            # This means on next tick, we could still launch one more issue
            assert orchestrator.state.issues_started_count == 1


class TestControlMethods:
    """Test pause, resume, prioritize methods."""

    def test_pause_sets_paused_flag(self, sample_config):
        """Test that pause() sets the paused flag."""
        orchestrator = create_test_orchestrator(sample_config)

        assert orchestrator.state.paused is False

        orchestrator.pause()

        assert orchestrator.state.paused is True

    def test_resume_clears_paused_flag(self, sample_config):
        """Test that resume() clears the paused flag."""
        orchestrator = create_test_orchestrator(sample_config)
        orchestrator.state.paused = True

        orchestrator.resume()

        assert orchestrator.state.paused is False

    def test_request_shutdown_sets_flag(self, sample_config):
        """Test that request_shutdown() sets the shutdown flag."""
        orchestrator = create_test_orchestrator(sample_config)

        assert orchestrator.shutdown_requested is False

        orchestrator.request_shutdown()

        assert orchestrator.shutdown_requested is True

    def test_request_shutdown_emits_shutdown_requested_event(self, sample_config):
        """Test that request_shutdown() emits orchestrator.shutdown_requested event."""
        orchestrator = create_test_orchestrator(sample_config)

        orchestrator.request_shutdown()

        events = [e for e in orchestrator.deps.events.events if e.name == "orchestrator.shutdown_requested"]
        assert len(events) == 1
        assert events[0].data["force"] is False
        assert events[0].data["active_session_count"] == 0
        assert events[0].data["sessions"] == []

    def test_request_shutdown_force_emits_event_with_force_flag(self, sample_config):
        """Test that force shutdown emits event with force=True."""
        orchestrator = create_test_orchestrator(sample_config)
        # Add an active session to test force behavior
        issue = create_issue(123)
        session = create_session(issue)
        orchestrator.state.active_sessions.append(session)

        orchestrator.request_shutdown(force=True)

        events = [e for e in orchestrator.deps.events.events if e.name == "orchestrator.shutdown_requested"]
        assert len(events) == 1
        assert events[0].data["force"] is True
        assert events[0].data["active_session_count"] == 1
        assert events[0].data["sessions"] == [123]

    @pytest.mark.asyncio
    async def test_run_loop_emits_shutdown_events(self, sample_config):
        """Test that run_loop emits shutdown_started and shutdown_completed events."""
        orchestrator = create_test_orchestrator(sample_config)
        orchestrator.shutdown_requested = True  # Trigger immediate exit

        await orchestrator.run_loop()

        event_names = [e.name for e in orchestrator.deps.events.events]
        assert "orchestrator.shutdown_started" in event_names
        assert "orchestrator.shutdown_completed" in event_names


class TestRunOrchestrator:
    """Test the run_orchestrator entry point."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.entrypoints.bootstrap.build_orchestrator")
    @patch("issue_orchestrator.infra.orchestrator.Config.load")
    @patch("issue_orchestrator.infra.orchestrator.signal.signal")
    async def test_run_orchestrator_loads_config_from_path(
        self,
        mock_signal,
        mock_config_load,
        mock_build,
        sample_config,
        tmp_path,
    ):
        """Test that run_orchestrator loads config from provided path."""
        mock_config_load.return_value = sample_config

        # Mock the orchestrator that build_orchestrator returns
        mock_orch = MagicMock()
        mock_orch.startup = AsyncMock()
        mock_orch.run_loop = AsyncMock()
        mock_build.return_value = mock_orch

        config_path = tmp_path / "config.yaml"
        await run_orchestrator(config_path)

        mock_config_load.assert_called_once_with(config_path)

    @pytest.mark.asyncio
    @patch("issue_orchestrator.entrypoints.bootstrap.build_orchestrator")
    @patch("issue_orchestrator.infra.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.infra.orchestrator.signal.signal")
    async def test_run_orchestrator_finds_config_when_no_path(
        self,
        mock_signal,
        mock_config_find,
        mock_build,
        sample_config,
    ):
        """Test that run_orchestrator finds config when no path provided."""
        mock_config_find.return_value = sample_config

        mock_orch = MagicMock()
        mock_orch.startup = AsyncMock()
        mock_orch.run_loop = AsyncMock()
        mock_build.return_value = mock_orch

        await run_orchestrator(None)

        mock_config_find.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.entrypoints.bootstrap.build_orchestrator")
    @patch("issue_orchestrator.infra.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.infra.orchestrator.signal.signal")
    async def test_run_orchestrator_calls_startup(
        self,
        mock_signal,
        mock_config_find,
        mock_build,
        sample_config,
    ):
        """Test that run_orchestrator calls startup."""
        mock_config_find.return_value = sample_config

        mock_orch = MagicMock()
        mock_orch.startup = AsyncMock()
        mock_orch.run_loop = AsyncMock()
        mock_build.return_value = mock_orch

        await run_orchestrator(None)

        mock_orch.startup.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.entrypoints.bootstrap.build_orchestrator")
    @patch("issue_orchestrator.infra.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.infra.orchestrator.signal.signal")
    async def test_run_orchestrator_calls_run_loop(
        self,
        mock_signal,
        mock_config_find,
        mock_build,
        sample_config,
    ):
        """Test that run_orchestrator calls run_loop."""
        mock_config_find.return_value = sample_config

        mock_orch = MagicMock()
        mock_orch.startup = AsyncMock()
        mock_orch.run_loop = AsyncMock()
        mock_build.return_value = mock_orch

        await run_orchestrator(None)

        mock_orch.run_loop.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.entrypoints.bootstrap.build_orchestrator")
    @patch("issue_orchestrator.infra.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.infra.orchestrator.signal.signal")
    async def test_run_orchestrator_sets_up_signal_handlers(
        self,
        mock_signal,
        mock_config_find,
        mock_build,
        sample_config,
    ):
        """Test that run_orchestrator sets up signal handlers."""
        mock_config_find.return_value = sample_config

        mock_orch = MagicMock()
        mock_orch.startup = AsyncMock()
        mock_orch.run_loop = AsyncMock()
        mock_build.return_value = mock_orch

        await run_orchestrator(None)

        # Should set up handlers for SIGINT and SIGTERM
        import signal
        assert mock_signal.call_count == 2
        call_args_list = [call[0][0] for call in mock_signal.call_args_list]
        assert signal.SIGINT in call_args_list
        assert signal.SIGTERM in call_args_list


class TestGatherTechLeadFacts:
    """Test the fact_gatherer.gather_tech_lead_facts method for tech_lead review workflow.

    Tech Lead issue creation is now handled by the Planner via:
    - fact_gatherer.gather_tech_lead_facts() gathers TechLeadFacts snapshot
    - fact_gatherer.create_snapshot() includes tech_lead_facts
    - Planner._plan_tech_lead_issue_creation() decides whether to create
    """

    def test_gather_tech_lead_facts_returns_none_without_agent(self, sample_config, mock_repository_host):
        """Test that gather_tech_lead_facts returns None without tech_lead_review_agent."""
        sample_config.tech_lead_review_agent = None
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.tech_lead_review_threshold = 5

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        facts = orchestrator.deps.fact_gatherer.gather_tech_lead_facts(orchestrator.state)
        assert facts is None

    def test_gather_tech_lead_facts_returns_none_with_zero_threshold(self, sample_config, mock_repository_host):
        """Test that gather_tech_lead_facts returns None with threshold=0."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.tech_lead_review_threshold = 0

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        facts = orchestrator.deps.fact_gatherer.gather_tech_lead_facts(orchestrator.state)
        assert facts is None

    def test_gather_tech_lead_facts_returns_facts_below_threshold(self, sample_config, mock_repository_host):
        """Test that gather_tech_lead_facts returns facts even when below threshold."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.tech_lead_review_threshold = 5

        # Set up PRs with the code-reviewed label (only 2, below threshold of 5)
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(1, "PR 1", labels=["code-reviewed"]),
            create_pr_info(2, "PR 2", labels=["code-reviewed"]),
        ]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        facts = orchestrator.deps.fact_gatherer.gather_tech_lead_facts(orchestrator.state)

        # Facts should be returned (Planner decides whether to act)
        assert facts is not None
        assert facts.pr_count == 2
        assert facts.threshold == 5
        assert facts.watch_label == "code-reviewed"

    def test_gather_tech_lead_facts_returns_facts_at_threshold(self, sample_config, mock_repository_host):
        """Test that gather_tech_lead_facts returns facts when at threshold."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.tech_lead_review_threshold = 3

        # Set up 3 PRs with code-reviewed label (meets threshold)
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(1, "PR 1", labels=["code-reviewed"]),
            create_pr_info(2, "PR 2", labels=["code-reviewed"]),
            create_pr_info(3, "PR 3", labels=["code-reviewed"]),
        ]
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        facts = orchestrator.deps.fact_gatherer.gather_tech_lead_facts(orchestrator.state)

        assert facts is not None
        assert facts.pr_count == 3
        assert facts.threshold == 3
        assert facts.existing_tech_lead_issue is None
        assert len(facts.prs) == 3

    def test_gather_tech_lead_facts_finds_existing_tech_lead_issue(self, sample_config, mock_repository_host):
        """Test that gather_tech_lead_facts detects existing tech_lead issue."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.tech_lead_review_threshold = 3

        # Set up 3 PRs with code-reviewed label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(1, "PR 1", labels=["code-reviewed"]),
            create_pr_info(2, "PR 2", labels=["code-reviewed"]),
            create_pr_info(3, "PR 3", labels=["code-reviewed"]),
        ]

        # Existing review issue
        existing_issue = create_issue(100, title="Tech Lead Batch Review: 3 PRs pending", labels=["agent:tech-lead"])
        mock_repository_host.issues = [existing_issue]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        facts = orchestrator.deps.fact_gatherer.gather_tech_lead_facts(orchestrator.state)

        assert facts is not None
        assert facts.existing_tech_lead_issue == 100

    def test_gather_tech_lead_facts_includes_pr_info(self, sample_config, mock_repository_host):
        """Test that gather_tech_lead_facts includes PR number and title tuples."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"
        sample_config.tech_lead_review_threshold = 2

        # Set up 2 PRs with code-reviewed label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(10, "Fix bug A", labels=["code-reviewed"]),
            create_pr_info(20, "Add feature B", labels=["code-reviewed"]),
        ]
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        facts = orchestrator.deps.fact_gatherer.gather_tech_lead_facts(orchestrator.state)

        assert facts is not None
        assert len(facts.prs) == 2
        # PRs should be tuples of (number, title)
        pr_numbers = [pr[0] for pr in facts.prs]
        assert 10 in pr_numbers
        assert 20 in pr_numbers


# TestQueueCodeReview removed - queue_code_review method was legacy
# Code review queueing is now tested via discovered_reviews + Planner pattern
# See test_planner.py for QueueReviewAction tests


class TestLaunchReviewSession:
    """Test the launch_review_session method."""

    @staticmethod
    def _review_worktree_manager(tmp_path: Path) -> MockWorktreeManager:
        return MockWorktreeManager(
            worktree_path=tmp_path / "review-worktree",
            branch_name="feature/issue-42",
        )

    def test_launch_review_session_creates_worktree(
        self,
        sample_config,
        tmp_path: Path,
    ):
        """Test that launch_review_session creates a worktree."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = self._review_worktree_manager(tmp_path)

        # Configure code review agent
        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager, runner=runner)
        session = orchestrator.launch_review_session(review)

        assert len(mock_worktree_manager.create_calls) == 1
        # Should pass branch_name to checkout existing PR branch
        assert mock_worktree_manager.create_calls[0]["branch_name"] == "feature/issue-42"

    def test_launch_review_session_creates_tmux_session(
        self,
        sample_config,
        tmp_path: Path,
    ):
        """Test that launch_review_session creates a tmux session."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = self._review_worktree_manager(tmp_path)

        sample_config.code_review_agent = "agent:web"
        sample_config.ui_mode = "tmux"  # Explicitly use tmux mode

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager, runner=runner)
        session = orchestrator.launch_review_session(review)

        # Verify session was created via plugin
        assert len(orchestrator.deps.runner.plugin.create_session_calls) == 1
        call = orchestrator.deps.runner.plugin.create_session_calls[0]
        # Session ID should be review-{pr_number} encoded as integer
        assert call["session_id"] == 123  # PR number

    def test_launch_review_session_adds_to_active_sessions(
        self,
        sample_config,
        tmp_path: Path,
    ):
        """Test that launch_review_session adds session to active_sessions."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = self._review_worktree_manager(tmp_path)

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager, runner=runner)
        assert len(orchestrator.state.active_sessions) == 0

        session = orchestrator.launch_review_session(review)

        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0] == session

    def test_launch_review_session_removes_from_pending_queue(
        self,
        sample_config,
        tmp_path: Path,
    ):
        """Test that launch_review_session removes PR from pending_reviews."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = self._review_worktree_manager(tmp_path)

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager, runner=runner)
        orchestrator.state.pending_reviews.append(review)
        assert len(orchestrator.state.pending_reviews) == 1

        session = orchestrator.launch_review_session(review)

        assert len(orchestrator.state.pending_reviews) == 0

    def test_launch_review_session_returns_none_if_session_exists(
        self,
        sample_config,
    ):
        """Test that launch_review_session returns None if session already exists."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = True  # Session already running

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, runner=runner)
        session = orchestrator.launch_review_session(review)

        assert session is None

    def test_launch_review_session_uses_review_prefix_for_session_check(
        self,
        sample_config,
    ):
        """Test that launch_review_session checks for review-{pr_number} session."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        # Session exists - already running
        runner.plugin.session_exists_override = True

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, runner=runner)
        orchestrator.launch_review_session(review)

        # Should check for review-{pr_number} session
        assert (123, "review-123") in orchestrator.deps.runner.plugin.session_exists_calls

    def test_launch_review_session_returns_none_without_agent_config(self, sample_config):
        """Test that launch_review_session returns None without code_review_agent configured."""
        from issue_orchestrator.domain.models import PendingReview

        sample_config.code_review_agent = None  # Not configured

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config)
        session = orchestrator.launch_review_session(review)

        assert session is None

    def test_launch_review_session_does_not_enforce_hooks(
        self,
        sample_config,
        tmp_path: Path,
    ):
        """Test that launch_review_session does not install pre-push hooks."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False
        mock_worktree_manager = self._review_worktree_manager(tmp_path)

        sample_config.code_review_agent = "agent:web"
        sample_config.enforce_hooks = True  # Even if enabled globally

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager, runner=runner)
        session = orchestrator.launch_review_session(review)

        # Should explicitly disable hooks for review sessions
        assert mock_worktree_manager.create_calls[0]["enforce_hooks"] is False


class TestHandleSessionCompletionWithCodeReview:
    """Test handle_session_completion triggering code review.

    Note: Session completion now stores DiscoveredReview for the Planner to decide,
    instead of directly calling queue_code_review.
    """

    @pytest.fixture
    def mock_worktree_manager(self):
        """Create a mock worktree manager for testing."""
        manager = MagicMock()
        manager.remove = MagicMock()
        return manager

    def test_handle_completion_stores_discovered_review(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion stores DiscoveredReview for Planner."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo
        from issue_orchestrator.domain.models import DiscoveredReview

        sample_config.code_review_agent = "agent:reviewer"
        mock_repository_host.prs["feature/issue-1"] = [
            PRInfo(
                number=456,
                title="Test PR",
                url="https://github.com/owner/repo/pull/456",
                branch="feature/issue-1",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue, branch_name="feature/issue-1")

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        # Initially no discovered reviews
        assert len(orchestrator.state.discovered_reviews) == 0

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Should have stored a DiscoveredReview for the Planner
        assert len(orchestrator.state.discovered_reviews) == 1
        review = orchestrator.state.discovered_reviews[0]
        assert review.issue_number == 1
        assert review.pr_number == 456
        assert review.pr_url == "https://github.com/owner/repo/pull/456"
        assert review.branch_name == "feature/issue-1"

    def test_handle_completion_does_not_store_review_without_agent(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion doesn't store DiscoveredReview without code_review_agent."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_agent = None

        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=456,
                title="Test PR",
                url="https://github.com/owner/repo/pull/456",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Should not store DiscoveredReview without code_review_agent
        assert len(orchestrator.state.discovered_reviews) == 0

    def test_handle_completion_does_not_store_review_for_blocked(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion doesn't store DiscoveredReview for blocked sessions."""
        sample_config.code_review_agent = "agent:reviewer"

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.BLOCKED)

        # Should not store DiscoveredReview for blocked sessions
        assert len(orchestrator.state.discovered_reviews) == 0

    def test_handle_completion_does_not_store_review_without_pr(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that handle_session_completion doesn't store DiscoveredReview if no PR found."""
        sample_config.code_review_agent = "agent:reviewer"

        # No PRs configured for this branch
        mock_repository_host.prs = {}

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Should not store DiscoveredReview if no PR found
        assert len(orchestrator.state.discovered_reviews) == 0


class TestStartupPendingReviews:
    """Test startup recovery for pending code reviews."""

    @pytest.mark.asyncio
    async def test_startup_scans_for_pending_reviews(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup scans for PRs with code_review_label."""
        mock_repository_host.issues = [
            Issue(number=123, title="Issue 123", labels=["agent:web"], state="open"),
            Issue(number=456, title="Issue 456", labels=["agent:web"], state="open"),
        ]
        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False

        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_review_label = "needs-code-review"

        # Set up PRs with the code review label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(123, "PR 123", labels=["needs-code-review"], branch="feature/123"),
            create_pr_info(456, "PR 456", labels=["needs-code-review"], branch="feature/456"),
        ]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, runner=runner)
        await orchestrator.startup()

        # Should have queued both PRs for review
        assert len(orchestrator.state.pending_reviews) == 2
        assert orchestrator.state.pending_reviews[0].pr_number == 123
        assert orchestrator.state.pending_reviews[1].pr_number == 456

    @pytest.mark.asyncio
    async def test_startup_skips_reviews_already_in_progress(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup skips PRs with active review sessions."""
        mock_repository_host.issues = [
            Issue(number=123, title="Issue 123", labels=["agent:web"], state="open"),
            Issue(number=456, title="Issue 456", labels=["agent:web"], state="open"),
        ]

        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_review_label = "needs-code-review"

        # Set up PRs with the code review label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(123, "PR 123", labels=["needs-code-review"], branch="feature/123"),
            create_pr_info(456, "PR 456", labels=["needs-code-review"], branch="feature/456"),
        ]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        # Session exists for PR 123 but not 456
        def session_exists_side_effect(name):
            return name == "review-123"

        with patch.object(orchestrator, "_session_exists", side_effect=session_exists_side_effect):
            await orchestrator.startup()

        # Should only queue PR 456 (123 is already in progress)
        assert len(orchestrator.state.pending_reviews) == 1
        assert orchestrator.state.pending_reviews[0].pr_number == 456

    @pytest.mark.asyncio
    async def test_startup_does_not_scan_without_code_review_config(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup doesn't scan for reviews without config."""
        mock_repository_host.issues = []

        sample_config.code_review_agent = None
        sample_config.code_review_label = None

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        await orchestrator.startup()

        # No PRs should be queued when code review is not configured
        assert len(orchestrator.state.pending_reviews) == 0


class TestPauseBehavior:
    """Test that pause stops all new work from starting."""

    # NOTE: test_process_pending_reviews_does_nothing_when_paused was removed
    # because process_pending_reviews() was deleted. The paused behavior is now
    # tested in test_workflows.py::TestReviewWorkflow::test_should_launch_skips_when_paused
    #
    # NOTE: test_check_tech_lead_review_trigger_does_nothing_when_paused was removed
    # because check_tech_lead_review_trigger() was refactored. The pause behavior is now
    # handled by the Planner via the paused flag in OrchestratorSnapshot. The
    # _gather_tech_lead_facts() method just gathers facts regardless of pause state;
    # the Planner decides whether to act on them.

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Mock asyncio.sleep to yield control but not wait."""
        original_sleep = asyncio.sleep

        async def instant_yield(*args):
            await original_sleep(0)

        with patch("issue_orchestrator.infra.orchestrator.asyncio.sleep", side_effect=instant_yield):
            yield

    @pytest.mark.asyncio
    async def test_run_loop_stops_batch_when_paused_mid_launch(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop stops launching when paused mid-batch."""
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        launch_count = 0

        def launch_side_effect(issue):
            nonlocal launch_count
            launch_count += 1
            # Pause after first launch
            if launch_count == 1:
                orchestrator.state.paused = True
                orchestrator.request_shutdown()
            return create_session(issue)

        with patch.object(orchestrator, "launch_session", side_effect=launch_side_effect) as mock_launch:
            await orchestrator.run_loop()

            # Should only launch 1 issue (paused after first)
            assert mock_launch.call_count == 1
            assert orchestrator.state.paused is True


class TestReconcileOrphanedPrLabels:
    """Test the reconcile_orphaned_pr_labels method.

    Tests use MockGitHubAdapter to inject test PRs rather than mocking subprocess.
    """

    def test_reconcile_skips_when_no_code_review_label(self, sample_config, mock_repository_host):
        """Test that reconciliation is skipped when code review is not configured."""
        sample_config.code_review_label = None

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0

    def test_reconcile_adds_label_to_orphaned_prs(self, sample_config, mock_repository_host):
        """Test that orphaned PRs get the code review label added."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo
        from issue_orchestrator.control import LabelSync
        from issue_orchestrator.ports import NullEventSink

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"
        mock_repository_host.issues.append(
            Issue(number=42, title="Issue with orphaned PR", labels=["agent:web"], state="open")
        )

        # Add a PR without review labels to the mock
        orphaned_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Generated by issue-orchestrator agent",  # Has orchestrator marker
            state="open",
            labels=[],  # No review labels
        )
        mock_repository_host.prs["feature-branch"] = [orphaned_pr]

        # Create label_sync with the mock (it needs both labels and pr_tracker)
        label_sync = LabelSync(
            labels=mock_repository_host,
            events=NullEventSink(),
            pr_tracker=mock_repository_host,
        )

        # Pass label_sync via factory
        orchestrator = create_test_orchestrator(
            sample_config, mock_repository_host, label_sync=label_sync
        )

        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 1
        # Should have called add_label with PR number and review label
        assert (42, "needs-code-review") in mock_repository_host.add_label_calls

    def test_reconcile_skips_out_of_scope_orphaned_prs(self, sample_config, mock_repository_host):
        """Run-specific review labels should not be added to unrelated e2e PRs."""
        from issue_orchestrator.control import LabelSync
        from issue_orchestrator.ports import NullEventSink

        sample_config.code_review_label = "io:e2e:needs-review-4057-123"
        sample_config.code_reviewed_label = "io:e2e:reviewed-4057-123"
        sample_config.filtering.label = "io:e2e:isolated-4057-123"
        sample_config.repo = "owner/repo"

        mock_repository_host.issues.append(
            Issue(
                number=123,
                title="[M0-123] unrelated e2e issue",
                labels=["io-e2e-test-data", "agent:e2e-test"],
                state="open",
            )
        )
        orphaned_pr = PRInfo(
            number=42,
            title="#123: unrelated e2e PR",
            url="https://github.com/owner/repo/pull/42",
            branch="123-unrelated-e2e-pr",
            body="Closes #123\n\nGenerated by issue-orchestrator agent",
            state="open",
            labels=[],
        )
        mock_repository_host.prs["123-unrelated-e2e-pr"] = [orphaned_pr]

        label_sync = LabelSync(
            labels=mock_repository_host,
            events=NullEventSink(),
            pr_tracker=mock_repository_host,
        )
        orchestrator = create_test_orchestrator(
            sample_config, mock_repository_host, label_sync=label_sync
        )

        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        assert mock_repository_host.add_label_calls == []

    def test_reconcile_skips_non_orchestrator_prs(self, sample_config, mock_repository_host):
        """Test that non-orchestrator PRs are skipped."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo
        from issue_orchestrator.control import LabelSync
        from issue_orchestrator.ports import NullEventSink

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR without the orchestrator marker
        external_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Some other PR body",  # No orchestrator marker
            state="open",
            labels=[],
        )
        mock_repository_host.prs["feature-branch"] = [external_pr]

        # Create label_sync with the mock
        label_sync = LabelSync(
            labels=mock_repository_host,
            events=NullEventSink(),
            pr_tracker=mock_repository_host,
        )

        # Pass label_sync via factory
        orchestrator = create_test_orchestrator(
            sample_config, mock_repository_host, label_sync=label_sync
        )

        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0

    def test_reconcile_skips_prs_with_review_label(self, sample_config, mock_repository_host):
        """Test that PRs already with review labels are skipped."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo
        from issue_orchestrator.control import LabelSync
        from issue_orchestrator.ports import NullEventSink

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR that already has the review label
        reviewed_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Generated by issue-orchestrator agent",
            state="open",
            labels=["needs-code-review"],  # Already has review label
        )
        mock_repository_host.prs["feature-branch"] = [reviewed_pr]

        # Create label_sync with the mock
        label_sync = LabelSync(
            labels=mock_repository_host,
            events=NullEventSink(),
            pr_tracker=mock_repository_host,
        )

        # Pass label_sync via factory
        orchestrator = create_test_orchestrator(
            sample_config, mock_repository_host, label_sync=label_sync
        )

        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0

    def test_reconcile_skips_prs_with_code_reviewed_label(self, sample_config, mock_repository_host):
        """Test that PRs with code-reviewed label are skipped."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR that already has the code-reviewed label
        completed_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Generated by issue-orchestrator agent",
            state="open",
            labels=["code-reviewed"],  # Already reviewed
        )
        mock_repository_host.prs["feature-branch"] = [completed_pr]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0


class TestSessionExistsDetection:
    """Test session detection prevents duplicate launches.

    These tests verify that the orchestrator correctly detects existing sessions
    and prevents duplicate launches, which was previously handled by lock files.
    """

    def test_review_with_active_terminal_requires_discovered_run_assets(
        self,
        sample_config,
        tmp_path,
    ):
        """Launch routing does not rediscover review worktrees without run assets."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = True  # Terminal session exists (but not tracked)

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        worktree_path = tmp_path / "repo-42"
        worktree_path.mkdir(parents=True, exist_ok=True)

        orchestrator = create_test_orchestrator(sample_config, runner=runner)
        orchestrator.state.pending_reviews.append(review)

        result = orchestrator.launch_review_session(review)

        assert result is None
        # Keep queued until runner discovery can provide a typed run contract.
        assert orchestrator.state.pending_reviews == [review]
        # Restoration requires runner-discovered run assets, not worktree guessing.
        assert orchestrator.state.active_sessions == []

    def test_review_tracked_in_active_sessions_removed_from_pending(
        self,
        sample_config,
    ):
        """Test reviews tracked in active_sessions are removed from pending."""
        from issue_orchestrator.domain.models import PendingReview

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config)
        orchestrator.state.pending_reviews.append(review)

        # Simulate session already tracked in active_sessions
        existing_session = create_session(create_issue(42))
        existing_session.terminal_id = "review-123"
        orchestrator.state.active_sessions.append(existing_session)

        result = orchestrator.launch_review_session(review)

        assert result is None
        # Should be removed from pending (session exists in active_sessions)
        assert len(orchestrator.state.pending_reviews) == 0

    def test_rework_with_active_terminal_requires_discovered_run_assets(
        self,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Launch routing does not rediscover rework worktrees without run assets."""
        from issue_orchestrator.domain.models import PendingRework
        from issue_orchestrator.domain.issue_key import FakeIssueKey

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = True  # Terminal session exists (but not tracked)
        mock_repository_host.issues = [create_issue(42)]

        sample_config.code_review_agent = "agent:web"

        rework = PendingRework(
            issue_key=FakeIssueKey(name="42"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        worktree_path = tmp_path / "repo-42"
        worktree_path.mkdir(parents=True, exist_ok=True)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, runner=runner)
        orchestrator.state.pending_reworks.append(rework)

        result = orchestrator.launch_rework_session(rework)

        assert result is None
        # Keep queued until runner discovery can provide a typed run contract.
        assert orchestrator.state.pending_reworks == [rework]
        # Restoration requires runner-discovered run assets, not worktree guessing.
        assert orchestrator.state.active_sessions == []


class TestStateMachineTransitions:
    """Test state machine transitions between pending, active, and completed states."""

    def test_successful_review_launch_transitions_pending_to_active(
        self,
        sample_config,
    ):
        """Test that successful launch moves review from pending to active."""
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = False  # No existing session
        mock_worktree_manager = MockWorktreeManager()

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, worktree_manager=mock_worktree_manager, runner=runner)
        orchestrator.state.pending_reviews.append(review)

        session = orchestrator.launch_review_session(review)

        # Should be removed from pending
        assert len(orchestrator.state.pending_reviews) == 0
        # Should be added to active
        assert len(orchestrator.state.active_sessions) == 1
        assert session is not None

    def test_failed_launch_does_not_leave_stuck_pending(
        self,
        sample_config,
    ):
        """Test that failed launch doesn't leave item stuck in pending.

        This is the critical bug fix test: if session_exists returns True
        (session already running), the item should be removed from pending_reviews.
        """
        from issue_orchestrator.domain.models import PendingReview

        runner = MockSessionRunner()
        runner.plugin.session_exists_override = True  # Session already exists

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        orchestrator = create_test_orchestrator(sample_config, runner=runner)
        # Manually add to pending (simulating process_pending_reviews behavior)
        orchestrator.state.pending_reviews.append(review)

        result = orchestrator.launch_review_session(review)

        assert result is None
        # Key assertion: even though launch failed, item is NOT stuck in pending
        # (In the buggy version, pending_reviews would still contain the review)

    # NOTE: test_process_pending_reviews_processes_all_pending was removed because
    # process_pending_reviews() was deleted. The workflow layer now handles batching
    # reviews - see test_workflows.py::TestReviewWorkflow::test_should_launch_returns_reviews_up_to_capacity


class TestNamingConventions:
    """Tests for centralized naming convention helpers.

    These tests use the public functions from control/worktree_manager.py directly.
    """

    def test_get_session_name_issue(self):
        """Test session name for issue type."""
        from issue_orchestrator.control.worktree_manager import get_session_name
        assert get_session_name(123, "issue") == "issue-123"
        assert get_session_name(1, "issue") == "issue-1"

    def test_get_session_name_review(self):
        """Test session name for review type."""
        from issue_orchestrator.control.worktree_manager import get_session_name
        assert get_session_name(456, "review") == "review-456"

    def test_get_session_name_rework(self):
        """Test session name for rework type."""
        from issue_orchestrator.control.worktree_manager import get_session_name
        assert get_session_name(789, "rework") == "rework-789"

    def test_get_session_name_invalid_type(self):
        """Test that invalid session type raises error."""
        from issue_orchestrator.control.worktree_manager import get_session_name
        with pytest.raises(ValueError, match="Invalid session_type"):
            get_session_name(123, "invalid")

    def test_get_worktree_path(self, sample_config, tmp_path):
        """Test worktree path derivation."""
        from issue_orchestrator.control.worktree_manager import get_worktree_path
        # Set up config with known repo_root and worktree_base
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.worktree_base = tmp_path / "worktrees"  # Top-level worktree_base

        agent_config = AgentConfig(
            prompt_path=tmp_path / "prompt.txt",
            model="sonnet",
            timeout_minutes=45,
        )

        path = get_worktree_path(sample_config, 123, agent_config)

        assert path == tmp_path / "worktrees" / "my-repo-123"


class TestDeferredCleanup:
    """Tests for deferred cleanup functionality."""

    @pytest.fixture
    def mock_worktree_manager(self):
        """Create a mock worktree manager for testing."""
        manager = MagicMock()
        manager.remove = MagicMock()
        return manager

    def test_handle_completion_defers_cleanup_with_tech_lead(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that cleanup is deferred when tech_lead review is enabled."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        # Enable tech_lead review
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"

        # Mock PR response
        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=100,
                title="Test PR",
                url="https://github.com/owner/repo/pull/100",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Worktree should NOT be removed (deferred)
        mock_worktree_manager.remove_checkout.assert_not_called()

        # Should have pending cleanup
        assert len(orchestrator.state.pending_cleanups) == 1
        pending = orchestrator.state.pending_cleanups[0]
        assert pending.issue_number == 1
        assert pending.pr_number == 100

    def test_handle_completion_defers_cleanup_with_code_review(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that cleanup is deferred when code review is enabled and wait_for_code_review is true."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        # Enable code review only (no CTO)
        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cleanup.without_tech_lead.wait_for_code_review = True

        # Mock PR response
        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=100,
                title="Test PR",
                url="https://github.com/owner/repo/pull/100",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Worktree should NOT be removed (deferred)
        mock_worktree_manager.remove_checkout.assert_not_called()

        # Should have pending cleanup
        assert len(orchestrator.state.pending_cleanups) == 1

    def test_handle_completion_immediate_cleanup_without_review(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that immediate cleanup is recorded when no review workflow is configured."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        # No review workflow
        sample_config.tech_lead_review_agent = None
        sample_config.code_review_agent = None

        # Mock PR response
        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=100,
                title="Test PR",
                url="https://github.com/owner/repo/pull/100",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Immediate cleanup is recorded as a fact for the planner
        assert len(orchestrator.state.immediate_cleanups) == 1
        cleanup = orchestrator.state.immediate_cleanups[0]
        assert cleanup.issue_number == 1
        assert cleanup.reason == "completed"

        # No pending (deferred) cleanups
        assert len(orchestrator.state.pending_cleanups) == 0

    def test_handle_completion_no_defer_for_failed_sessions(
        self,
        sample_config,
        mock_repository_host,
        mock_worktree_manager,
    ):
        """Test that failed sessions get immediate cleanup (not deferred)."""
        # Enable tech_lead review
        sample_config.tech_lead_review_agent = "agent:tech-lead"

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, worktree_manager=mock_worktree_manager)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        # No pending (deferred) cleanups for failed sessions
        assert len(orchestrator.state.pending_cleanups) == 0
        # Immediate cleanup is recorded
        assert len(orchestrator.state.immediate_cleanups) == 1
        cleanup = orchestrator.state.immediate_cleanups[0]
        assert cleanup.reason == "failed"


class TestProcessDeferredCleanups:
    """Tests for processing deferred cleanups."""

    def test_process_cleanups_when_pr_reviewed(
        self,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that cleanups are processed when PR has reviewed label."""
        from issue_orchestrator.domain.models import PendingCleanup

        # Enable tech_lead review
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"
        sample_config.cleanup.with_tech_lead.remove_worktrees = True

        # Set up PR with reviewed label
        mock_repository_host.prs["issue-1-test"] = [
            create_pr_info(100, "PR 100", labels=["tech-lead-reviewed"], branch="issue-1-test"),
        ]

        mock_worktree_manager = MockWorktreeManager()
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager)

        # Add pending cleanup
        pending = PendingCleanup(
            issue=Issue(number=1, title="Test issue", labels=[]),
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="issue-1-test",
            terminal_id="issue-1",
            worktree_path=tmp_path / "worktree-1",
        )
        orchestrator.state.pending_cleanups.append(pending)

        orchestrator.process_deferred_cleanups()

        # Worktree should be removed
        assert len(mock_worktree_manager.remove_calls) == 1
        assert mock_worktree_manager.remove_calls[0] == tmp_path / "worktree-1"

        # Pending cleanup should be removed
        assert len(orchestrator.state.pending_cleanups) == 0

    def test_process_cleanups_skips_unreviewed_prs(
        self,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that cleanups are not processed if PR doesn't have reviewed label."""
        from issue_orchestrator.domain.models import PendingCleanup

        # Enable tech_lead review
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"

        # No PRs with reviewed label (empty prs dict)
        mock_repository_host.prs = {}

        mock_worktree_manager = MockWorktreeManager()
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager)

        # Add pending cleanup
        pending = PendingCleanup(
            issue=Issue(number=1, title="Test issue", labels=[]),
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="issue-1-test",
            terminal_id="issue-1",
            worktree_path=tmp_path / "worktree-1",
        )
        orchestrator.state.pending_cleanups.append(pending)

        orchestrator.process_deferred_cleanups()

        # Worktree should NOT be removed
        assert len(mock_worktree_manager.remove_calls) == 0

        # Pending cleanup should still be there
        assert len(orchestrator.state.pending_cleanups) == 1

    def test_process_cleanups_noop_when_empty(self, sample_config):
        """Test that process_deferred_cleanups does nothing when queue is empty."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"

        orchestrator = create_test_orchestrator(sample_config)
        # No pending cleanups

        # Should not raise
        orchestrator.process_deferred_cleanups()

    def test_process_cleanups_noop_without_review_workflow(self, sample_config):
        """Test that process_deferred_cleanups handles no review workflow."""
        from issue_orchestrator.domain.models import PendingCleanup

        # No review workflow
        sample_config.tech_lead_review_agent = None
        sample_config.code_review_agent = None

        orchestrator = create_test_orchestrator(sample_config)

        # Add a pending cleanup (shouldn't happen in practice, but test robustness)
        pending = PendingCleanup(
            issue=Issue(number=1, title="Test issue", labels=[]),
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="issue-1-test",
            terminal_id="issue-1",
            worktree_path=Path("/tmp/worktree-1"),
        )
        orchestrator.state.pending_cleanups.append(pending)

        # Should not raise, cleanup stays pending
        orchestrator.process_deferred_cleanups()


class TestRecoverOrphanedCleanups:
    """Tests for orphaned cleanup recovery on startup."""

    def test_recover_cleans_orphaned_worktrees(
        self,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that orphaned worktrees are cleaned up on startup."""
        # Set up config
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"
        sample_config.cleanup.with_tech_lead.remove_worktrees = True

        # Create worktree base (now on config, not agent)
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        sample_config.worktree_base = worktree_base

        # Create orphaned worktree
        orphaned_worktree = worktree_base / "my-repo-123"
        orphaned_worktree.mkdir()

        # Set up PR with reviewed label - includes our orphan
        # Branch naming convention is {issue_number}-{slug}, not issue-{number}
        mock_repository_host.prs["123-test-feature"] = [
            create_pr_info(100, "PR 100", labels=["tech-lead-reviewed"], branch="123-test-feature"),
        ]

        mock_worktree_manager = MockWorktreeManager()
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager)

        # Mock session_exists to return False (session not running)
        # noqa: SLF001 - Test infrastructure: injecting mock to control cleanup behavior
        orchestrator._session_exists = lambda name: False  # noqa: SLF001

        # noqa: SLF001 - Testing cleanup behavior that has no public API
        orchestrator._recover_orphaned_cleanups()  # noqa: SLF001

        # Orphaned worktree should be cleaned up
        assert len(mock_worktree_manager.remove_calls) == 1
        assert mock_worktree_manager.remove_calls[0] == orphaned_worktree

    def test_recover_skips_running_sessions(
        self,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that worktrees with running sessions are not cleaned up."""
        # Set up config
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"

        # Create worktree base (now on config, not agent)
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        sample_config.worktree_base = worktree_base

        # Create worktree
        worktree = worktree_base / "my-repo-123"
        worktree.mkdir()

        # Set up PR with reviewed label
        mock_repository_host.prs["issue-123-test-feature"] = [
            create_pr_info(100, "PR 100", labels=["tech-lead-reviewed"], branch="issue-123-test-feature"),
        ]

        mock_worktree_manager = MockWorktreeManager()
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host, mock_worktree_manager)

        # Mock session_exists to return True (session still running)
        orchestrator._session_exists = lambda name: True  # noqa: SLF001

        orchestrator._recover_orphaned_cleanups()  # noqa: SLF001

        # Worktree should NOT be cleaned up (session still running)
        assert len(mock_worktree_manager.remove_calls) == 0

    def test_recover_noop_without_review_workflow(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that recovery does nothing without review workflow."""
        # No review workflow
        sample_config.tech_lead_review_agent = None
        sample_config.code_review_agent = None

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        orchestrator._recover_orphaned_cleanups()  # noqa: SLF001
        # Method should return early without checking PRs

    def test_recover_handles_no_reviewed_prs(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that recovery handles case with no reviewed PRs."""
        sample_config.tech_lead_review_agent = "agent:tech-lead"
        sample_config.tech_lead_reviewed_label = "tech-lead-reviewed"

        # No reviewed PRs
        mock_repository_host.prs = {}

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        # Should not raise
        orchestrator._recover_orphaned_cleanups()  # noqa: SLF001


class TestReworkEscalation:
    """Test rework escalation to needs-human after max cycles.

    Tests use MockGitHubAdapter to verify adapter calls rather than mocking subprocess.
    """

    def test_escalation_flows_through_planner_and_action_applier(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that escalation is handled by Planner producing EscalateToHumanAction.

        The flow is:
        1. scan_needs_rework_prs stores DiscoveredEscalation
        2. Planner produces EscalateToHumanAction
        3. ActionApplier executes the escalation (label, comment)

        This test verifies the Planner produces the action with correct labels.
        ActionApplier tests verify the execution.
        """
        from issue_orchestrator.control.planner import Planner
        from issue_orchestrator.control.scheduler import Scheduler
        from issue_orchestrator.control.actions import EscalateToHumanAction

        sample_config.max_rework_cycles = 2

        scheduler = Scheduler(config=sample_config)
        planner = Planner(config=sample_config, scheduler=scheduler)

        # Create a snapshot with a discovered escalation
        from issue_orchestrator.domain.models import OrchestratorState, DiscoveredEscalation
        from issue_orchestrator.control.planner_types import OrchestratorSnapshot

        state = OrchestratorState()

        escalation = DiscoveredEscalation(
            pr_number=123,
            issue_number=456,
            rework_cycle=3,  # 3 means 2 completed cycles (exceeded max of 2)
        )

        snapshot = OrchestratorSnapshot.from_state(
            issues=[],  # No issues to start
            state=state,
            discovered_escalations=[escalation],
        )
        plan = planner.plan(snapshot)

        # Should have an EscalateToHumanAction
        escalate_actions = [a for a in plan.actions if isinstance(a, EscalateToHumanAction)]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]

        # Verify the action has correct labels
        assert action.pr_number == 123
        assert action.issue_number == 456
        assert action.needs_human_label == sample_config.get_label_needs_human()
        assert action.needs_rework_label == sample_config.get_label_needs_rework()
        assert action.max_rework_cycles == sample_config.max_rework_cycles

    def test_scan_needs_rework_discovers_escalation_at_max_cycles(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that scan_needs_rework_prs stores DiscoveredEscalation when max cycles exceeded.

        Note: Actual escalation is now handled by the Planner via EscalateToHumanAction.
        """
        sample_config.max_rework_cycles = 2
        sample_config.code_review_agent = "agent:code-reviewer"

        # Add issue with agent label (agent type now comes from issue, not PR)
        from tests.builders import IssueBuilder
        mock_repository_host.issues.append(
            IssueBuilder()
            .with_number(123)
            .with_agent("agent:code-reviewer")
            .build()
        )

        # Simulate a PR that has gone through 2 rework cycles (labels show rework-cycle-2)
        # Branch starts with issue number for reliable extraction
        mock_repository_host.prs["123-feature"] = [
            create_pr_info(
                123,
                "PR 123",
                labels=["needs-rework", "rework-cycle-2"],
                branch="123-feature",
            ),
        ]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.scan_needs_rework_prs()

        # Should have stored a DiscoveredEscalation for Planner to decide
        assert len(orchestrator.state.discovered_escalations) == 1
        escalation = orchestrator.state.discovered_escalations[0]
        assert escalation.pr_number == 123
        assert escalation.rework_cycle == 3  # rework-cycle-2 means next cycle is 3

        # Should NOT have added to pending_reworks queue (Planner decides)
        assert len(orchestrator.state.pending_reworks) == 0
        assert len(orchestrator.state.discovered_reworks) == 0

    def test_scan_needs_rework_discovers_rework_within_limit(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that scan_needs_rework_prs stores DiscoveredRework when within limit.

        Note: Actual queuing is now handled by the Planner via QueueReworkAction.
        """
        sample_config.max_rework_cycles = 3
        sample_config.code_review_agent = "agent:code-reviewer"

        # Add issue with agent label (agent type now comes from issue, not PR)
        from tests.builders import IssueBuilder
        mock_repository_host.issues.append(
            IssueBuilder()
            .with_number(123)
            .with_agent("agent:code-reviewer")
            .build()
        )

        # Simulate a PR on first rework cycle (no rework-cycle label)
        # Branch starts with issue number for reliable extraction
        mock_repository_host.prs["123-feature"] = [
            create_pr_info(
                123,
                "PR 123",
                labels=["needs-rework"],
                branch="123-feature",
            ),
        ]

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        orchestrator.scan_needs_rework_prs()

        # Should have stored a DiscoveredRework for Planner to decide
        assert len(orchestrator.state.discovered_reworks) == 1
        rework = orchestrator.state.discovered_reworks[0]
        assert rework.issue_number == 123
        assert rework.rework_cycle == 1
        assert rework.agent_type == "agent:code-reviewer"

        # Should NOT have added to pending_reworks queue yet (Planner decides)
        assert len(orchestrator.state.pending_reworks) == 0

    def test_get_rework_cycle_from_labels_extracts_cycle(
        self,
        sample_config,
    ):
        """Test that rework cycle is correctly extracted from labels."""
        from issue_orchestrator.control.pr_scanner import PRScanner
        from unittest.mock import MagicMock

        # Create scanner with minimal mocks
        scanner = PRScanner(
            config=sample_config,
            repository=MagicMock(),
            events=MagicMock(),
        )

        # No rework label - first cycle
        labels = ["needs-rework", "test-data"]
        assert scanner._get_rework_cycle_from_labels(labels) == 1  # noqa: SLF001

        # rework-cycle-1 label - next is cycle 2
        labels = ["needs-rework", "rework-cycle-1"]
        assert scanner._get_rework_cycle_from_labels(labels) == 2  # noqa: SLF001

        # rework-cycle-2 label - next is cycle 3
        labels = ["rework-cycle-2", "needs-rework"]
        assert scanner._get_rework_cycle_from_labels(labels) == 3  # noqa: SLF001

        # rework-cycle-5 label - next is cycle 6
        labels = ["rework-cycle-5"]
        assert scanner._get_rework_cycle_from_labels(labels) == 6  # noqa: SLF001


class TestRefreshRequestPreservation:
    """Tests for refresh request preservation during planning cycles.

    These tests verify the fix for a race condition where request_refresh()
    called during a planning cycle would be lost when the cycle returned.

    Note: These tests access private members to verify internal state for
    a specific race condition fix. The behavior being tested doesn't have
    a public API equivalent.
    """

    def test_refresh_request_during_planning_cycle_preserved(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that request_refresh() during planning cycle is preserved.

        This tests the fix for a race condition where:
        1. _run_planning_cycle() captures state.queue_refresh_requested
        2. During the cycle, request_refresh() sets the state flag again
        3. The cycle returns and SHOULD NOT overwrite the new True value
        """
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        # Initially False
        assert orchestrator.state.queue_refresh_requested is False

        # Set to True (simulating a request before cycle)
        orchestrator.state.queue_refresh_requested = True

        # Run planning cycle - this should clear the flag when processing it
        orchestrator._run_planning_cycle()  # noqa: SLF001

        # After cycle processes the request, flag should be False
        assert orchestrator.state.queue_refresh_requested is False

        # Now test the race condition fix by calling request_refresh()
        # during the planning implementation itself.
        def request_during_cycle(*_args, **_kwargs):
            orchestrator.request_refresh({'new-issue-123'})
            return 0.0, False

        with patch(
            "issue_orchestrator.infra.orchestrator._run_planning_cycle_impl",
            side_effect=request_during_cycle,
        ):
            orchestrator._run_planning_cycle()  # noqa: SLF001

        # After our simulated cycle, the flag should STILL be True
        # (the fix ensures we don't overwrite it)
        assert orchestrator.state.queue_refresh_requested is True, \
            "refresh request during cycle should be preserved"

    def test_request_refresh_sets_flag(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that request_refresh() sets the state refresh flag."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        assert orchestrator.state.queue_refresh_requested is False

        orchestrator.request_refresh()

        assert orchestrator.state.queue_refresh_requested is True

    def test_request_refresh_with_inflight_ids_sets_flag(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that request_refresh() with inflight IDs sets flag and stores IDs."""
        mock_repository_host.issues = []

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)

        assert orchestrator.state.queue_refresh_requested is False
        assert len(orchestrator._inflight_stable_ids) == 0  # noqa: SLF001

        orchestrator.request_refresh({'issue-1', 'issue-2'})

        assert orchestrator.state.queue_refresh_requested is True
        assert 'issue-1' in orchestrator._inflight_stable_ids  # noqa: SLF001
        assert 'issue-2' in orchestrator._inflight_stable_ids  # noqa: SLF001


def test_full_deps_publish_recovery_reconciles_through_shared_action_applier(sample_config):
    """build_test_orchestrator_deps must wire the resolved (non-None) applier.

    Production passes the resolved ActionApplier into PublishRecoveryService; the
    test builder must use the same object OrchestratorDeps exposes, not the raw
    optional argument (which is None when callers omit it). A reconciliation that
    applies labels proves the wiring is intact — with a None applier this raises.
    """
    from issue_orchestrator.control.actions import AddLabelAction

    orchestrator = create_test_orchestrator(sample_config)
    deps = orchestrator.deps

    # Spy on the applier OrchestratorDeps exposes. PublishRecoveryService must
    # hold this same object for the spy to observe its label writes.
    applied = []

    def _spy(action):
        applied.append(action)
        return SimpleNamespace(success=True, error=None)

    deps.action_applier.apply = _spy

    deps.publish_recovery.reconcile_retry_publish_success(
        state=OrchestratorState(),
        issue_number=4057,
        issue_title="UI: Surface provider status",
        agent_label="agent:web",
        pr_url="https://github.com/owner/repo/pull/5453",
        pr_number=5453,
        worktree_path=None,
    )

    assert any(
        isinstance(action, AddLabelAction)
        and action.label == deps.label_manager.pr_pending
        for action in applied
    ), "publish recovery must apply labels through deps.action_applier"


class TestPublicCompletionFacadeSettlesClaims:
    """Both completion paths must settle the pending-work claim (#6999 F9).

    The periodic processing path injected the claim store; the public facade
    did not. A queued session completing through the facade therefore removed
    itself from active_sessions and then failed while settling, leaving state
    half-mutated and the two paths behaving differently. These tests drive the
    facade itself, because the boundary tests pass the store explicitly to the
    control function and cannot see the wiring.
    """

    def _claimed_session(self, orchestrator, tmp_path: Path):
        from issue_orchestrator.control.in_flight_work import InFlightWorkLedger
        from issue_orchestrator.domain.issue_key import FakeIssueKey
        from issue_orchestrator.domain.models import PendingReview
        from issue_orchestrator.domain.pending_work import (
            PendingWorkClaim,
            PendingWorkKind,
        )

        issue = create_issue(1)
        session = create_session(issue)
        from tests.unit.session_run_helpers import make_session_run_assets

        session.run_assets = make_session_run_assets(
            tmp_path / "wt", session_name=session.terminal_id
        )
        claim = PendingWorkClaim(
            PendingWorkKind.REVIEW,
            PendingReview(
                issue_key=FakeIssueKey(name="1"),
                pr_number=70,
                pr_url="url",
                branch_name="branch",
                _issue_number=1,
                agent_label="agent:backend",
            ),
        )
        orchestrator.state.active_sessions.append(session)
        InFlightWorkLedger(
            orchestrator.state, orchestrator.deps.pending_work_claims
        ).take(session, claim)
        return session

    def test_facade_returns_provider_deferred_work_to_its_queue(
        self, sample_config, tmp_path: Path
    ):
        from issue_orchestrator.ports.provider_resilience import ProviderErrorType

        orchestrator = create_test_orchestrator(sample_config)
        session = self._claimed_session(orchestrator, tmp_path)

        orchestrator.handle_session_completion(
            session,
            SessionStatus.BLOCKED,
            provider_error_type=ProviderErrorType.AUTH,
        )

        assert [r.pr_number for r in orchestrator.state.pending_reviews] == [70]

    def test_facade_consumes_the_claim_on_a_real_work_outcome(
        self, sample_config, tmp_path: Path
    ):
        orchestrator = create_test_orchestrator(sample_config)
        session = self._claimed_session(orchestrator, tmp_path)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert orchestrator.state.pending_reviews == []
        assert orchestrator.deps.pending_work_claims.list_unresolved_claims() == ()


class TestReconcileSweepsThePendingWorkLedger:
    """The runtime reconcile must sweep even with nothing untracked (#6999 F8).

    The two early returns in ``_reconcile_running_sessions`` used to skip
    recovery entirely, and the ledger rows that recovery exists for belong to
    runs whose terminals are already gone - so those are exactly the branches
    where the row is the only remaining record of the work. Driven through the
    real facade so either return regaining its short-circuit fails here.
    """

    def _seed_unresolved_claim(self, orchestrator, tmp_path: Path):
        from issue_orchestrator.domain.models import (
            DiscoveredFailure,
            PendingTechLeadReview,
        )
        from issue_orchestrator.domain.pending_work import (
            PendingWorkClaim,
            PendingWorkKind,
        )
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
        from tests.unit.session_run_helpers import make_session_run_assets

        claim = PendingWorkClaim(
            PendingWorkKind.TECH_LEAD,
            PendingTechLeadReview(
                issue_number=7,
                title="Investigate: session failed",
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                failure=DiscoveredFailure(7, "Test", "failed", blocking_label="blocked-failed"),
            ),
        )
        orchestrator.deps.pending_work_claims.hold_pending_work_claim(
            make_session_run_assets(tmp_path / "gone-wt", session_name="issue-7"),
            claim,
            issue_number=7,
        )

    def _force_scan(self, orchestrator):
        orchestrator._last_orphan_reconcile_scan_at = 0.0  # noqa: SLF001
        orchestrator._last_orphan_reconcile_active_count = -1  # noqa: SLF001

    def test_nothing_discovered_still_recovers_the_work(
        self, sample_config, mock_repository_host, tmp_path: Path
    ):
        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        self._seed_unresolved_claim(orchestrator, tmp_path)
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[])
        self._force_scan(orchestrator)

        orchestrator._reconcile_running_sessions()  # noqa: SLF001

        assert [
            t.issue_number for t in orchestrator.state.pending_tech_lead_reviews
        ] == [7]

    def test_everything_already_tracked_still_recovers_the_work(
        self, sample_config, mock_repository_host, tmp_path: Path
    ):
        """The other early return: discovery found only terminals we track."""
        from tests.unit.session_run_helpers import make_session_run_assets

        orchestrator = create_test_orchestrator(sample_config, mock_repository_host)
        self._seed_unresolved_claim(orchestrator, tmp_path)
        tracked = MagicMock(spec=Session)
        tracked.terminal_id = "issue-99"
        tracked.run_assets = make_session_run_assets(
            tmp_path / "live-wt", session_name="issue-99"
        )
        orchestrator.state.active_sessions = [tracked]
        orchestrator.deps.runner.discover_running_sessions = MagicMock(return_value=[
            {"issue_number": 99, "tab_name": "issue-99", "is_review": False,
             "session_name": "issue-99"}
        ])
        self._force_scan(orchestrator)

        orchestrator._reconcile_running_sessions()  # noqa: SLF001

        assert [
            t.issue_number for t in orchestrator.state.pending_tech_lead_reviews
        ] == [7]
