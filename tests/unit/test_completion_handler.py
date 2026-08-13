"""Comprehensive behavior-centric tests for CompletionHandler.

These tests verify the behaviors of CompletionHandler which handles:
1. Session completion state machine updates
2. Event emission for trace events
3. History entry creation
4. Cleanup decision logic
5. Label/comment action generation

Tests focus on invariant outcomes, state transitions, and business rules
rather than implementation details.
"""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, Mock

from issue_orchestrator.infra.config import (
    Config,
    CleanupConfig,
    CleanupWithTechLead,
    CleanupWithoutTechLead,
)
from issue_orchestrator.control.completion_handler import (
    CleanupDecision,
    CleanupDisposition,
    CompletionHandler,
    CompletionResult,
)
from issue_orchestrator.control.actions import (
    AddLabelAction,
    RemoveLabelAction,
    AddCommentAction,
    SetIssueStateAction,
    ActionType,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.state_machines.issue_machine import IssueStateMachine, IssueState
from issue_orchestrator.domain.state_machines.session_machine import SessionStateMachine, SessionState
from issue_orchestrator.domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus, PendingCleanup
from issue_orchestrator.ports import NullEventSink, InMemoryEventSink, TraceEvent
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.events import EventName
from issue_orchestrator.contracts.public import SessionCompletedPayload
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from tests.conftest import make_provider_availability
from tests.unit.session_run_helpers import make_session_run_assets


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def config() -> Config:
    """Create a minimal valid config."""
    cfg = Config()
    cfg.repo = "owner/repo"
    return cfg


@pytest.fixture
def tmp_worktree(tmp_path: Path) -> Path:
    """Create a temporary worktree directory."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return worktree


@pytest.fixture
def agent_config(tmp_path: Path) -> AgentConfig:
    """Create a minimal agent config."""
    return AgentConfig(
        prompt_path=tmp_path / "prompt.txt",
        timeout_minutes=45,
    )


def make_issue(number: int = 1, title: str = "Test issue", labels: list[str] | None = None, repo: str = "owner/repo") -> Issue:
    """Create a test issue."""
    return Issue(
        number=number,
        title=title,
        labels=labels or ["agent:test"],
        repo=repo,
    )


def create_test_session(
    issue: Issue,
    agent_config: AgentConfig,
    worktree_path: Path,
    terminal_id: str = "issue-1",
    task_kind: TaskKind = TaskKind.CODE,
    run_assets: SessionRunAssets | None = None,
) -> Session:
    """Create a test session."""
    issue_key = FakeIssueKey(str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task_kind)
    pr_number: int | None = None
    if terminal_id.startswith(("review-", "rework-")):
        try:
            pr_number = int(terminal_id.split("-", 1)[1])
        except (ValueError, IndexError):
            pr_number = None
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=terminal_id,
        worktree_path=worktree_path,
        branch_name=f"issue-{issue.number}",
        run_assets=run_assets
        or make_session_run_assets(worktree_path, session_name=terminal_id),
        pr_number=pr_number,
    )


def make_repository_host(
    prs: list[Any] | None = None,
    pr_info: Any | None = None,
    issue_info: Any | None = None,
) -> SimpleNamespace:
    """Create a mock repository host."""
    issue_value = issue_info if issue_info is not None else SimpleNamespace(labels=[])
    return SimpleNamespace(
        get_prs_for_branch=lambda _branch: prs or [],
        get_pr=lambda _pr_number: pr_info,
        get_issue=lambda _issue_number: issue_value,
        get_issue_labels_fresh=lambda _issue_number: [str(label) for label in getattr(issue_value, "labels", [])],
        set_pr_draft=Mock(),
    )


def make_handler(
    config: Config,
    events: Any | None = None,
    repository_host: Any | None = None,
    issue_machine: Any | None = None,
    session_machine: Any | None = None,
    review_machine: Any | None = None,
    session_output: SessionOutput | None = None,
) -> CompletionHandler:
    """Create a CompletionHandler with sensible defaults."""
    default_session_output = Mock(spec=SessionOutput)
    default_session_output.find_run_dir.return_value = None
    # Use explicit None check - InMemoryEventSink with 0 events is falsy
    # Tech-Lead-configured tests rendezvous with recorded authority through the
    # SQLite adapter at config.repo_root (a tmp_path); everything else gets
    # the in-memory port fake so no state files are written.
    from issue_orchestrator.infra.tech_lead_authority_store import (
        SqliteTechLeadAuthorityStore,
    )
    from issue_orchestrator.ports.tech_lead_authority import (
        InMemoryTechLeadAuthorityStore,
    )
    from issue_orchestrator.ports.open_issue_corpus_store import (
        InMemoryOpenIssueCorpusStore,
    )

    tech_lead_authority = (
        SqliteTechLeadAuthorityStore.for_repo(config.repo_root)
        if config.tech_lead_review_agent
        else InMemoryTechLeadAuthorityStore()
    )
    from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager

    resolved_repository_host = repository_host or make_repository_host()
    return CompletionHandler(
        config=config,
        events=events if events is not None else NullEventSink(),
        repository_host=resolved_repository_host,
        get_issue_machine_fn=lambda _issue: issue_machine,
        get_session_machine_fn=lambda _terminal_id: session_machine,
        get_review_machine_fn=lambda _pr_number: review_machine,
        session_output=session_output if session_output is not None else default_session_output,
        tech_lead_authority=tech_lead_authority,
        open_issue_corpus=OpenIssueCorpusManager(
            resolved_repository_host,
            InMemoryOpenIssueCorpusStore(),
            is_enabled=lambda: config.tech_lead.dedup.enabled,
        ),
        active_session_run_id=lambda _n: None,
        provider_availability=make_provider_availability(config),
    )


# =============================================================================
# Test: History Entry Creation
# =============================================================================


class TestHistoryEntryCreation:
    """Tests for history entry creation on session completion."""

    def test_completed_session_creates_history_with_pr_url(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session with PR records the PR URL in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.history_entry.pr_url == pr_url
        assert result.history_entry.status == "completed"
        assert result.history_entry.issue_number == issue.number

    def test_failed_session_creates_history_without_pr_url(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed session has no PR URL in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        assert result.history_entry.pr_url is None
        assert result.history_entry.status == "failed"
        assert "without PR" in result.history_entry.status_reason

    def test_timed_out_session_records_timeout_info(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Timed out session records timeout information in history."""
        agent_config.timeout_minutes = 30
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.TIMED_OUT)

        assert result.history_entry.status == "timed_out"
        assert "30" in result.history_entry.status_reason
        assert "timeout" in result.history_entry.status_reason.lower()

    def test_validation_failed_session_records_validation_failure(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Validation-failed sessions keep their terminal history status."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.VALIDATION_FAILED)

        assert result.history_entry.status == "validation_failed"
        assert "validation" in result.history_entry.status_reason.lower()

    def test_blocked_session_records_blocked_reason(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session records blocked status in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.BLOCKED)

        assert result.history_entry.status == "blocked"
        assert "blocked" in result.history_entry.status_reason.lower()

    def test_needs_human_session_records_reason(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session records human intervention reason."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert result.history_entry.status == "needs_human"
        assert "human" in result.history_entry.status_reason.lower()

    def test_completed_with_critical_errors_records_failed_status(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Agent reported completed but push/PR failed shows FAILED in history."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUSH

        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        # Agent said completed, but push failed
        processing_errors = [f"{ERROR_PREFIX_PUSH}: Push failed: branch too large"]

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=processing_errors,
        )

        # History should show FAILED, not COMPLETED
        assert result.history_entry.status == "failed"
        assert "push" in result.history_entry.status_reason.lower() or "pr" in result.history_entry.status_reason.lower()

    def test_completed_with_publish_blocked_error_records_failed_status(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Publish-blocked finalize failures (dirty-tree gate) should be FAILED in history."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUBLISH_BLOCKED

        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        processing_errors = [
            f"{ERROR_PREFIX_PUBLISH_BLOCKED}: Working tree is dirty; commit/add/stash before pushing."
        ]

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=processing_errors,
        )

        assert result.history_entry.status == "failed"
        assert "push" in result.history_entry.status_reason.lower() or "pr" in result.history_entry.status_reason.lower()

    def test_completed_without_errors_records_completed_status(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Successful completion with no errors shows COMPLETED in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr/42", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        # No processing errors - successful completion
        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.history_entry.status == "completed"
        assert "PR created" in result.history_entry.status_reason

    def test_completed_with_create_pr_error_but_existing_pr_records_completed_status(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """A create_pr error is non-critical if a PR exists by reconciliation time."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_CREATE_PR

        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[f"{ERROR_PREFIX_CREATE_PR}: GitHub request failed: 422"],
        )

        assert result.history_entry.status == "completed"
        assert result.history_entry.pr_url == pr_url
        assert "PR created" in result.history_entry.status_reason


# =============================================================================
# Test: Event Emission
# =============================================================================


class TestEventEmission:
    """Tests for trace event emission during completion processing."""

    def test_completed_session_emits_session_completed_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session emits SESSION_COMPLETED event with PR info."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert events.has_event(str(EventName.SESSION_COMPLETED))
        event = events.last_event(str(EventName.SESSION_COMPLETED))
        assert event is not None
        assert event.data["issue_number"] == issue.number
        assert event.data["pr_url"] == pr_url
        SessionCompletedPayload.model_validate(event.data)

    def test_failed_session_emits_session_failed_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed session emits SESSION_FAILED event with error info."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.FAILED)

        assert events.has_event(str(EventName.SESSION_FAILED))
        event = events.last_event(str(EventName.SESSION_FAILED))
        assert event is not None
        assert event.data["issue_number"] == issue.number
        assert "error" in event.data

    def test_failed_session_emits_recorded_run_dir_without_lookup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """SESSION_FAILED should include the launch-recorded run_dir."""
        events = InMemoryEventSink()
        issue = make_issue()
        run_assets = make_session_run_assets(
            tmp_worktree,
            session_name="issue-1",
            run_id="20260220-000000Z",
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            run_assets=run_assets,
        )
        session_output = Mock(spec=SessionOutput)
        handler = make_handler(config, events=events, session_output=session_output)

        handler.process_completion(session, SessionStatus.FAILED)

        event = events.last_event(str(EventName.SESSION_FAILED))
        assert event is not None
        assert event.data["run_dir"] == str(run_assets.run_dir)
        session_output.find_run_dir.assert_not_called()

    def test_failed_session_event_uses_recorded_run_assets(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Phase-named run dirs appear only when injected as owned run assets."""
        events = InMemoryEventSink()
        issue = make_issue(number=123)
        run_assets = make_session_run_assets(
            tmp_worktree,
            session_name="coding-1",
            run_id="20260525",
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="issue-123",
            run_assets=run_assets,
        )
        session_output = Mock(spec=SessionOutput)
        handler = make_handler(config, events=events, session_output=session_output)

        handler.process_completion(session, SessionStatus.FAILED)

        event = events.last_event(str(EventName.SESSION_FAILED))
        assert event is not None
        assert event.data["run_dir"] == str(run_assets.run_dir)
        session_output.find_run_dir.assert_not_called()

    def test_timed_out_session_emits_session_failed_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Timed out session emits SESSION_FAILED event."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        assert events.has_event(str(EventName.SESSION_FAILED))

    def test_blocked_session_emits_issue_blocked_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session emits ISSUE_BLOCKED event."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.BLOCKED)

        assert events.has_event(str(EventName.ISSUE_BLOCKED))
        event = events.last_event(str(EventName.ISSUE_BLOCKED))
        assert event is not None
        assert event.data["issue_number"] == issue.number

    def test_needs_human_session_emits_issue_needs_human_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session emits ISSUE_NEEDS_HUMAN event."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert events.has_event(str(EventName.ISSUE_NEEDS_HUMAN))

    def test_pr_view_changed_event_emitted_for_completed_with_pr(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """PR_VIEW_CHANGED event is emitted when a PR is discovered."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=["some-label"])]
        )
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert events.has_event(str(EventName.PR_VIEW_CHANGED))
        event = events.last_event(str(EventName.PR_VIEW_CHANGED))
        assert event is not None
        assert event.data["pr_number"] == 42
        assert event.data["labels"] == ["some-label"]


# =============================================================================
# Test: State Machine Transitions
# =============================================================================


class TestStateMachineTransitions:
    """Tests for state machine transitions during completion processing."""

    def test_completed_issue_session_transitions_to_pr_pending(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Issue session with PR transitions issue state to PR_PENDING."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config, repository_host=repository_host, issue_machine=issue_machine
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert issue_machine.get_state() == IssueState.PR_PENDING

    def test_pr_created_ignored_when_issue_already_pr_pending(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Duplicate pr_created transitions are ignored for issue sessions."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.PR_PENDING)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config, repository_host=repository_host, issue_machine=issue_machine
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        # State should remain PR_PENDING (no transition)
        assert issue_machine.get_state() == IssueState.PR_PENDING

    def test_review_session_does_not_trigger_pr_created(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review sessions don't trigger pr_created on the issue machine."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config, repository_host=repository_host, issue_machine=issue_machine
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        # Issue state should remain IN_PROGRESS (review sessions don't change it)
        assert issue_machine.get_state() == IssueState.IN_PROGRESS

    def test_blocked_session_transitions_issue_to_blocked(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session transitions issue to BLOCKED state."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree)

        handler = make_handler(config, issue_machine=issue_machine)

        handler.process_completion(session, SessionStatus.BLOCKED)

        assert issue_machine.get_state() == IssueState.BLOCKED

    def test_needs_human_session_transitions_issue_to_needs_human(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session transitions issue to NEEDS_HUMAN state."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree)

        handler = make_handler(config, issue_machine=issue_machine)

        handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert issue_machine.get_state() == IssueState.NEEDS_HUMAN

    def test_session_machine_transitions_on_completion(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session state machine transitions to COMPLETED on success."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(config, session_machine=session_machine)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert session_machine.get_state() == SessionState.COMPLETED

    def test_session_machine_transitions_on_failure(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session state machine transitions to FAILED on failure."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(config, session_machine=session_machine)

        handler.process_completion(session, SessionStatus.FAILED)

        assert session_machine.get_state() == SessionState.FAILED

    def test_session_machine_transitions_on_timeout(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session state machine transitions to TIMED_OUT on timeout."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(config, session_machine=session_machine)

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        assert session_machine.get_state() == SessionState.TIMED_OUT


# =============================================================================
# Test: Review Session Machine Transitions
# =============================================================================


class TestReviewMachineTransitions:
    """Tests for review state machine transitions on review session completion."""

    def test_review_session_with_code_reviewed_label_approves_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review session with code-reviewed label approves the review."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )
        review_machine = ReviewStateMachine(
            pr_number=42,
            issue_number=issue.number,
            initial_state=ReviewState.IN_REVIEW,
        )

        pr_info = SimpleNamespace(number=42, labels=["code-reviewed"], url="http://pr", draft=True)
        repository_host = make_repository_host(
            prs=[pr_info],
            pr_info=pr_info,
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            review_machine=review_machine,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert review_machine.get_state() == ReviewState.APPROVED
        repository_host.set_pr_draft.assert_called_once_with(42, False)

    def test_review_session_with_needs_rework_label_requests_changes_and_queues_rework(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review session with needs-rework label requests changes and queues rework."""
        config.label_needs_rework = "needs-rework"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )
        review_machine = ReviewStateMachine(
            pr_number=42,
            issue_number=issue.number,
            initial_state=ReviewState.IN_REVIEW,
        )

        pr_info = SimpleNamespace(number=42, labels=["needs-rework"], url="http://pr")
        repository_host = make_repository_host(
            prs=[pr_info],
            pr_info=pr_info,
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            review_machine=review_machine,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        # After request_changes and queue_rework
        assert review_machine.get_state() == ReviewState.REWORK_PENDING

    def test_review_session_no_transition_when_machine_not_found(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """No error when review machine is not found for a review session."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        pr_info = SimpleNamespace(number=42, labels=["code-reviewed"], url="http://pr")
        repository_host = make_repository_host(
            prs=[pr_info],
            pr_info=pr_info,
        )
        # No review machine provided (None)
        handler = make_handler(config, repository_host=repository_host)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None


# =============================================================================
# Test: PR Detection and Handling
# =============================================================================


class TestPRDetection:
    """Tests for PR detection from completed sessions."""

    def test_completed_session_detects_pr_from_branch(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session fetches and returns PR info from branch."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr/123", number=123, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.pr_url == "http://pr/123"
        assert result.pr_number == 123

    def test_completed_session_without_pr_returns_none(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session without PR returns None for pr_url and pr_number."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(prs=[])
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.pr_url is None
        assert result.pr_number is None

    def test_failed_session_does_not_fetch_prs(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed sessions don't fetch PRs."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        fetch_called = []
        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _: (fetch_called.append(True), [])[1],
            get_pr=lambda _: None,
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.FAILED)

        assert not fetch_called
        assert result.pr_url is None

    def test_multiple_prs_uses_first_one(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """When multiple PRs exist for branch, the first one is used."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(prs=[
            SimpleNamespace(url="http://pr/1", number=1, labels=[]),
            SimpleNamespace(url="http://pr/2", number=2, labels=[]),
        ])
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.pr_url == "http://pr/1"
        assert result.pr_number == 1


# =============================================================================
# Test: Cleanup Strategy Determination
# =============================================================================


class TestCleanupStrategy:
    """Tests for cleanup strategy determination."""

    @pytest.mark.parametrize(
        ("close_tabs", "remove_worktrees", "expected_disposition"),
        [
            pytest.param(
                True, True, CleanupDisposition.DEFERRED, id="both-actions"
            ),
            pytest.param(
                True, False, CleanupDisposition.DEFERRED, id="close-tabs-only"
            ),
            pytest.param(
                False, True, CleanupDisposition.DEFERRED, id="remove-worktree-only"
            ),
            pytest.param(False, False, CleanupDisposition.NONE, id="no-actions"),
        ],
    )
    def test_tech_lead_review_gate_is_independent_of_cleanup_actions(
        self,
        config: Config,
        agent_config: AgentConfig,
        tmp_worktree: Path,
        close_tabs: bool,
        remove_worktrees: bool,
        expected_disposition: CleanupDisposition,
    ) -> None:
        """Either selected cleanup action waits for tech-lead review."""
        config.tech_lead_review_agent = "agent:tech-lead"
        config.cleanup.with_tech_lead.close_ai_session_tabs = close_tabs
        config.cleanup.with_tech_lead.remove_worktrees = remove_worktrees
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.cleanup.disposition is expected_disposition
        if result.cleanup.pending_cleanup is not None:
            assert result.cleanup.pending_cleanup.pr_number == 42

    @pytest.mark.parametrize(
        (
            "wait_for_review",
            "close_tabs",
            "remove_worktrees",
            "expected_disposition",
        ),
        [
            pytest.param(
                True, True, True, CleanupDisposition.DEFERRED,
                id="wait-both-actions",
            ),
            pytest.param(
                True, True, False, CleanupDisposition.DEFERRED,
                id="wait-close-tabs-only",
            ),
            pytest.param(
                True, False, True, CleanupDisposition.DEFERRED,
                id="wait-remove-worktree-only",
            ),
            pytest.param(
                True, False, False, CleanupDisposition.NONE,
                id="wait-no-actions",
            ),
            pytest.param(
                False, True, True, CleanupDisposition.IMMEDIATE,
                id="immediate-both-actions",
            ),
            pytest.param(
                False, True, False, CleanupDisposition.IMMEDIATE,
                id="immediate-close-tabs-only",
            ),
            pytest.param(
                False, False, True, CleanupDisposition.IMMEDIATE,
                id="immediate-remove-worktree-only",
            ),
            pytest.param(
                False, False, False, CleanupDisposition.NONE,
                id="immediate-no-actions",
            ),
        ],
    )
    def test_code_review_timing_is_independent_of_cleanup_actions(
        self,
        config: Config,
        agent_config: AgentConfig,
        tmp_worktree: Path,
        wait_for_review: bool,
        close_tabs: bool,
        remove_worktrees: bool,
        expected_disposition: CleanupDisposition,
    ) -> None:
        """The wait flag controls timing; action flags control cleanup work."""
        config.tech_lead_review_agent = None
        config.code_review_agent = "agent:reviewer"
        config.cleanup.without_tech_lead.wait_for_code_review = wait_for_review
        config.cleanup.without_tech_lead.close_ai_session_tabs = close_tabs
        config.cleanup.without_tech_lead.remove_worktrees = remove_worktrees
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.cleanup.disposition is expected_disposition

    def test_disabled_tech_lead_uses_code_review_cleanup_without_erasing_agent(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead.enabled = False
        config.code_review_agent = "agent:reviewer"
        config.cleanup.without_tech_lead.wait_for_code_review = True
        config.cleanup.without_tech_lead.close_ai_session_tabs = True
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree, terminal_id="issue-1"
        )
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )

        result = make_handler(
            config, repository_host=repository_host
        ).process_completion(session, SessionStatus.COMPLETED)

        assert result.cleanup.disposition is CleanupDisposition.DEFERRED
        assert result.cleanup.pending_cleanup is not None

    def test_review_session_does_not_defer_cleanup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review sessions don't create pending cleanups."""
        config.tech_lead_review_agent = "agent:tech-lead"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.cleanup.disposition is CleanupDisposition.IMMEDIATE
        assert result.cleanup.pending_cleanup is None

    def test_rework_session_does_not_defer_cleanup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Rework sessions don't create pending cleanups."""
        config.tech_lead_review_agent = "agent:tech-lead"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="rework-42",
            task_kind=TaskKind.REWORK,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.cleanup.disposition is CleanupDisposition.IMMEDIATE

    @pytest.mark.parametrize(
        "status",
        [
            SessionStatus.FAILED,
            SessionStatus.TIMED_OUT,
            SessionStatus.BLOCKED,
        ],
    )
    def test_noncompleted_session_cleanup_remains_immediate_when_actions_disabled(
        self,
        config: Config,
        agent_config: AgentConfig,
        tmp_worktree: Path,
        status: SessionStatus,
    ) -> None:
        """Failure lifecycle teardown is independent of completion settings."""
        config.tech_lead_review_agent = "agent:tech-lead"
        config.cleanup.with_tech_lead.close_ai_session_tabs = False
        config.cleanup.with_tech_lead.remove_worktrees = False
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        handler = make_handler(config)

        result = handler.process_completion(session, status)

        assert result.cleanup.disposition is CleanupDisposition.IMMEDIATE
        assert result.cleanup.pending_cleanup is None

    def test_pending_cleanup_contains_correct_info(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """PendingCleanup object contains all required information."""
        config.tech_lead_review_agent = "agent:tech-lead"
        config.cleanup.with_tech_lead.close_ai_session_tabs = True
        issue = make_issue(number=123)
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="issue-123",
        )

        pr_url = "http://github.com/owner/repo/pull/456"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=456, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        cleanup = result.cleanup.pending_cleanup
        assert cleanup is not None
        assert cleanup.issue_number == 123
        assert cleanup.pr_number == 456
        assert cleanup.pr_url == pr_url
        assert cleanup.terminal_id == "issue-123"
        assert cleanup.branch_name == "issue-123"


# =============================================================================
# Test: Review Queue Decision
# =============================================================================


class TestReviewQueueDecision:
    """Tests for code review queueing decisions."""

    def test_completed_with_pr_and_code_review_agent_queues_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session with PR and code_review_agent configured queues review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is True

    def test_completed_without_code_review_agent_does_not_queue(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session without code_review_agent does not queue review."""
        config.code_review_agent = None
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    def test_skip_review_agent_config_prevents_queue(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Agent with skip_review=True does not queue review."""
        config.code_review_agent = "agent:reviewer"
        agent_config.skip_review = True
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    def test_review_session_does_not_queue_another_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review sessions don't queue another review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    def test_completed_without_pr_does_not_queue_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session completed without PR does not queue review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(prs=[])
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    @pytest.mark.parametrize("mode", ["via-mcp", "via-local-loop", "auto"])
    def test_loop_modes_skip_review_queue(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path, mode: str
    ) -> None:
        """Loop review modes should not queue PR review."""
        config.code_review_agent = "agent:reviewer"
        config.review_exchange_mode = mode
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            review_exchange_completed=True,
        )

        assert result.should_queue_review is False

    def test_review_exchange_halt_skips_review_queue(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Halted review exchange must not enqueue another review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue(number=123)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-123")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=["review_exchange: stopped (reviewer_reports_no_progress)"],
        )

        assert result.should_queue_review is False

    def test_review_exchange_halt_marks_issue_blocked_failed(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Halted review exchange must place issue on hold (blocked-failed) and release claim."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue(number=123, labels=["agent:test", "in-progress"])
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-123")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=["review_exchange: stopped (reviewer_reports_no_progress)"],
        )

        add_labels = [a for a in result.actions if isinstance(a, AddLabelAction)]
        remove_labels = [a for a in result.actions if isinstance(a, RemoveLabelAction)]

        assert any(action.label == "blocked-failed" for action in add_labels)
        assert any(action.label == "in-progress" for action in remove_labels)
        assert not any(action.label == "pr-pending" for action in add_labels)

    def test_review_exchange_halt_emits_failed_not_completed_events(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Halted review exchange should not emit completed/pr-created timeline events."""
        issue = make_issue(number=123)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-123")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        events = InMemoryEventSink()
        handler = make_handler(config, events=events, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=["review_exchange: stopped (reviewer_reports_no_progress)"],
        )

        assert result.history_entry.status == SessionStatus.FAILED.value
        assert events.has_event(str(EventName.SESSION_FAILED))
        assert not events.has_event(str(EventName.SESSION_COMPLETED))
        assert not events.has_event(str(EventName.ISSUE_PR_CREATED))


# =============================================================================
# Test: Label Action Generation
# =============================================================================


class TestLabelActionGeneration:
    """Tests for label/comment action generation on completion."""

    def test_review_exchange_completed_adds_pr_pending_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review exchange completion should add pr-pending when PR exists."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue(number=123)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-123")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            review_exchange_completed=True,
        )

        add_labels = [a for a in result.actions if isinstance(a, AddLabelAction)]
        assert any(action.label == "pr-pending" for action in add_labels)

    def test_retrospective_review_approval_flips_issue_labels(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(
            number=365,
            labels=[
                "agent:web",
                "agent:reviewer",
                "lack-of-review-redo",
                "retrospective-changes-requested",
            ],
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="retrospective-review-365",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            completion_detail={"outcome": "review_approved"},
        )

        removed = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        added = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        assert "lack-of-review-redo" in removed
        assert "retrospective-changes-requested" in removed
        assert "retrospective-reviewed" in added
        assert not any(isinstance(a, SetIssueStateAction) for a in result.actions)

    def test_retrospective_review_changes_requested_reopens_for_rework(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(
            number=365,
            labels=["agent:web", "agent:reviewer", "lack-of-review-redo"],
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="retrospective-review-365",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            completion_detail={"outcome": "review_changes_requested"},
        )

        removed = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        added = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        state_actions = [a for a in result.actions if isinstance(a, SetIssueStateAction)]
        assert "lack-of-review-redo" in removed
        assert "retrospective-reviewed" in removed
        assert "needs-rework" in removed
        assert "retrospective-changes-requested" in added
        assert len(state_actions) == 1
        assert state_actions[0].issue_number == 365
        assert state_actions[0].state == "open"

    def test_retrospective_review_changes_requested_without_coder_escalates_to_human(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(
            number=365,
            labels=["agent:reviewer", "lack-of-review-redo"],
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="retrospective-review-365",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )
        session.agent_label = "agent:reviewer"
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            completion_detail={"outcome": "review_changes_requested"},
        )

        added = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        removed = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        comments = [a.comment for a in result.actions if isinstance(a, AddCommentAction)]
        state_actions = [a for a in result.actions if isinstance(a, SetIssueStateAction)]
        assert "needs-rework" in removed
        assert "retrospective-changes-requested" in added
        assert "needs-human" in added
        assert any("could not resolve a coder `agent:*` label" in comment for comment in comments)
        assert len(state_actions) == 1
        assert state_actions[0].state == "open"

    def test_retrospective_review_approval_clears_blocking_labels(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(
            number=365,
            labels=[
                "agent:web",
                "agent:reviewer",
                "lack-of-review-redo",
                "blocked",
                "blocked-failed",
            ],
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="retrospective-review-365",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            completion_detail={"outcome": "review_approved"},
        )

        removed = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        added = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        # An approved retrospective review unblocks the issue.
        assert "blocked" in removed
        assert "blocked-failed" in removed
        assert "retrospective-reviewed" in added

    def test_retrospective_review_changes_requested_clears_blocking_labels(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(
            number=365,
            labels=["agent:web", "agent:reviewer", "lack-of-review-redo", "blocked"],
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="retrospective-review-365",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            completion_detail={"outcome": "review_changes_requested"},
        )

        removed = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        added = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        state_actions = [a for a in result.actions if isinstance(a, SetIssueStateAction)]
        # Changes-requested reopens for rework and must not leave the issue blocked.
        assert "blocked" in removed
        assert "retrospective-changes-requested" in added
        assert state_actions and state_actions[0].state == "open"

    def test_retrospective_review_needs_human_clears_prior_blocking_keeps_needs_human(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(
            number=365,
            labels=[
                "agent:reviewer",
                "lack-of-review-redo",
                "blocked-failed",
                "needs-human",
            ],
        )
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="retrospective-review-365",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )
        session.agent_label = "agent:reviewer"
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            completion_detail={"outcome": "review_changes_requested"},
        )

        removed = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        added = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        # Prior blocking labels are cleared, but the human escalation re-asserts
        # needs-human as the terminal blocked state (no remove/re-add churn).
        assert "blocked-failed" in removed
        assert "needs-human" in added
        assert "needs-human" not in removed

    def test_needs_run_audit_label_writes_audit_and_flips_labels(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(number=123, labels=["agent:test", "needs-run-audit"])
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_worktree, "issue-1", issue_number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            run_assets=run,
        )
        session.started_at = datetime.now().replace(microsecond=0)
        session_output.update_manifest(
            run.run_dir,
            {
                "outcome": "completed",
                "runtime_minutes": 12,
                "ended_at": "2026-03-14T23:55:16Z",
            },
        )

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _branch: [],
            get_pr=lambda _pr_number: None,
            get_issue=lambda _issue_number: SimpleNamespace(labels=["agent:test", "needs-run-audit"]),
            get_issue_labels_fresh=lambda _issue_number: ["agent:test", "needs-run-audit"],
            set_pr_draft=Mock(),
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            session_output=session_output,
        )

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        remove_labels = [a.label for a in result.actions if isinstance(a, RemoveLabelAction)]
        add_labels = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        assert "needs-run-audit" in remove_labels
        assert "run-audit-complete" in add_labels

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        audit_path = Path(manifest["run_audit_path"])
        assert audit_path.exists()
        audit_payload = audit_path.read_text()
        assert "needs-run-audit" in audit_payload

    def test_long_runtime_writes_automatic_run_audit_without_label_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(number=123, labels=["agent:test"])
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_worktree, "issue-1", issue_number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            run_assets=run,
        )
        session.started_at = (datetime.now() - timedelta(minutes=24)).replace(microsecond=0)
        session_output.update_manifest(run.run_dir, {"outcome": "completed", "ended_at": "2026-03-14T23:55:16Z"})

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _branch: [],
            get_pr=lambda _pr_number: None,
            get_issue=lambda _issue_number: SimpleNamespace(labels=["agent:test"]),
            get_issue_labels_fresh=lambda _issue_number: ["agent:test"],
            set_pr_draft=Mock(),
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            session_output=session_output,
        )

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert not any(
            isinstance(action, (RemoveLabelAction, AddLabelAction))
            and action.label in {"needs-run-audit", "run-audit-complete"}
            for action in result.actions
        )

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        audit_path = Path(manifest["run_audit_path"])
        payload = audit_path.read_text()
        assert "\"trigger_source\": \"runtime-threshold\"" in payload

    def test_runtime_below_threshold_does_not_write_automatic_run_audit(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(number=123, labels=["agent:test"])
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_worktree, "issue-1", issue_number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            run_assets=run,
        )
        session.started_at = (datetime.now() - timedelta(minutes=12)).replace(microsecond=0)
        session_output.update_manifest(run.run_dir, {"outcome": "completed", "ended_at": "2026-03-14T23:55:16Z"})

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _branch: [],
            get_pr=lambda _pr_number: None,
            get_issue=lambda _issue_number: SimpleNamespace(labels=["agent:test"]),
            get_issue_labels_fresh=lambda _issue_number: ["agent:test"],
            set_pr_draft=Mock(),
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            session_output=session_output,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        assert "run_audit_path" not in manifest

    def test_timeout_writes_automatic_run_audit(self, config: Config, agent_config: AgentConfig, tmp_worktree: Path) -> None:
        issue = make_issue(number=123, labels=["agent:test"])
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_worktree, "issue-1", issue_number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            run_assets=run,
        )
        session.started_at = (datetime.now() - timedelta(minutes=5)).replace(microsecond=0)
        session_output.update_manifest(run.run_dir, {"outcome": "timed_out", "ended_at": "2026-03-14T23:55:16Z"})

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _branch: [],
            get_pr=lambda _pr_number: None,
            get_issue=lambda _issue_number: SimpleNamespace(labels=["agent:test"]),
            get_issue_labels_fresh=lambda _issue_number: ["agent:test"],
            set_pr_draft=Mock(),
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            session_output=session_output,
        )

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        audit_path = Path(manifest["run_audit_path"])
        payload = audit_path.read_text()
        assert "\"trigger_source\": \"timeout\"" in payload

    def test_timeout_enriches_recorded_run_manifest_before_audit(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        issue = make_issue(number=123, labels=["agent:test"])
        agent_config.timeout_minutes = 90
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_worktree, "coding-1", issue_number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="issue-123",
            run_assets=run,
        )
        session.started_at = (datetime.now() - timedelta(minutes=91)).replace(microsecond=0)

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _branch: [],
            get_pr=lambda _pr_number: None,
            get_issue=lambda _issue_number: SimpleNamespace(labels=["agent:test"]),
            get_issue_labels_fresh=lambda _issue_number: ["agent:test"],
            set_pr_draft=Mock(),
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            session_output=session_output,
        )

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        assert manifest["outcome"] == "timed_out"
        assert manifest["runtime_minutes"] >= 90
        assert manifest["timeout_minutes"] == 90
        assert "ended_at" in manifest
        audit_payload = Path(manifest["run_audit_path"]).read_text()
        assert "\"outcome\": \"timed_out\"" in audit_payload

    def test_timeout_audit_can_be_disabled(self, config: Config, agent_config: AgentConfig, tmp_worktree: Path) -> None:
        config.review_run_audit_on_timeout = False
        config.review_run_audit_min_runtime_minutes = 20
        issue = make_issue(number=123, labels=["agent:test"])
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_worktree, "issue-1", issue_number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            run_assets=run,
        )
        session.started_at = (datetime.now() - timedelta(minutes=5)).replace(microsecond=0)
        session_output.update_manifest(run.run_dir, {"outcome": "timed_out", "ended_at": "2026-03-14T23:55:16Z"})

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _branch: [],
            get_pr=lambda _pr_number: None,
            get_issue=lambda _issue_number: SimpleNamespace(labels=["agent:test"]),
            get_issue_labels_fresh=lambda _issue_number: ["agent:test"],
            set_pr_draft=Mock(),
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            session_output=session_output,
        )

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        assert "run_audit_path" not in manifest

    def test_timeout_generates_blocked_failed_label_and_comment(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Timeout generates blocked-failed label, comment, and removes in-progress."""
        agent_config.timeout_minutes = 30
        issue = make_issue(number=123)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.TIMED_OUT)

        actions = result.actions
        assert len(actions) == 3

        # Find each action type
        add_label = next((a for a in actions if isinstance(a, AddLabelAction)), None)
        comment = next((a for a in actions if isinstance(a, AddCommentAction)), None)
        remove_label = next((a for a in actions if isinstance(a, RemoveLabelAction)), None)

        assert add_label is not None
        assert add_label.label == "blocked-failed"
        assert add_label.issue_number == 123

        assert comment is not None
        assert "Timed Out" in comment.comment
        assert "30" in comment.comment  # timeout minutes

        assert remove_label is not None
        assert remove_label.label == config.get_label_in_progress()

    def test_publish_blocked_error_generates_blocked_failed_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed+publish-blocked should yield publish-failed + comment + release claim."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUBLISH_BLOCKED

        issue = make_issue(number=321)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[
                f"{ERROR_PREFIX_PUBLISH_BLOCKED}: Working tree is dirty; commit/add/stash before pushing."
            ],
        )

        actions = result.actions
        assert any(isinstance(a, AddLabelAction) and a.label == "publish-failed" for a in actions)
        assert any(isinstance(a, RemoveLabelAction) and a.label == config.get_label_in_progress() for a in actions)
        assert any(
            isinstance(a, AddCommentAction) and "Publishing Failed" in a.comment
            for a in actions
        )

    def test_publish_failure_removes_needs_rework_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Publish failure should remove needs-rework to prevent re-queuing loop."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUBLISH_BLOCKED

        issue = make_issue(number=321)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[
                f"{ERROR_PREFIX_PUBLISH_BLOCKED}: push failed"
            ],
        )

        actions = result.actions
        remove_rework = [
            a for a in actions
            if isinstance(a, RemoveLabelAction) and a.label == "needs-rework"
        ]
        assert len(remove_rework) == 1, "Should remove needs-rework on publish failure"

    def test_create_pr_error_with_existing_pr_does_not_generate_publish_failed_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Recovered create_pr errors should not mark the issue publish-failed."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_CREATE_PR

        issue = make_issue(number=321)
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[f"{ERROR_PREFIX_CREATE_PR}: GitHub request failed: 422"],
        )

        actions = result.actions
        assert not any(
            isinstance(a, AddLabelAction) and a.label == "publish-failed"
            for a in actions
        )
        assert not any(
            isinstance(a, AddLabelAction) and a.label.startswith("publish-fail-count-")
            for a in actions
        )

    def test_publish_failure_adds_count_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """First publish failure should add publish-fail-count-1 label."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUBLISH_BLOCKED

        issue = make_issue(number=321)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[f"{ERROR_PREFIX_PUBLISH_BLOCKED}: push failed"],
        )

        actions = result.actions
        assert any(
            isinstance(a, AddLabelAction) and a.label == "publish-fail-count-1"
            for a in actions
        ), "Should add publish-fail-count-1 on first failure"

    def test_publish_failure_increments_count_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Second publish failure should replace count-1 with count-2."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUBLISH_BLOCKED

        issue = make_issue(number=321, labels=["agent:test", "publish-fail-count-1"])
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[f"{ERROR_PREFIX_PUBLISH_BLOCKED}: push failed"],
        )

        actions = result.actions
        assert any(
            isinstance(a, RemoveLabelAction) and a.label == "publish-fail-count-1"
            for a in actions
        ), "Should remove old count label"
        assert any(
            isinstance(a, AddLabelAction) and a.label == "publish-fail-count-2"
            for a in actions
        ), "Should add incremented count label"

    def test_publish_failure_escalates_after_max(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """After max consecutive publish failures, escalate to needs-human."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUBLISH_BLOCKED

        config.max_consecutive_publish_failures = 3
        # Issue already has count-2 (two previous failures)
        issue = make_issue(number=321, labels=["agent:test", "publish-fail-count-2"])
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[f"{ERROR_PREFIX_PUBLISH_BLOCKED}: push failed"],
        )

        actions = result.actions
        # Should escalate to needs-human, NOT publish-failed
        assert any(
            isinstance(a, AddLabelAction) and a.label == "needs-human"
            for a in actions
        ), "Should escalate to needs-human after max failures"
        assert not any(
            isinstance(a, AddLabelAction) and a.label == "publish-failed"
            for a in actions
        ), "Should NOT add publish-failed when escalating"
        assert any(
            isinstance(a, AddCommentAction) and "Escalated" in a.comment
            for a in actions
        ), "Comment should mention escalation"

    def test_failure_generates_blocked_needs_human_label_and_comment(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failure (no completion) generates blocked-needs-human label for investigation."""
        config.retry.interrupted_sessions.enabled = False
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        actions = result.actions

        add_label = next((a for a in actions if isinstance(a, AddLabelAction)), None)
        comment = next((a for a in actions if isinstance(a, AddCommentAction)), None)
        remove_label = next((a for a in actions if isinstance(a, RemoveLabelAction)), None)

        assert add_label is not None
        assert add_label.label == "needs-human"  # Needs human investigation

        assert comment is not None
        assert "Investigation" in comment.comment  # Explains coding-done/reviewer-done was not called

        assert remove_label is not None
        assert remove_label.label == config.get_label_in_progress()

    def test_invalid_completion_record_uses_specific_failure_comment_and_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Invalid completion JSON is not described as a missing command."""
        config.retry.interrupted_sessions.enabled = False
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree)
        completion_path = tmp_worktree / ".issue-orchestrator" / "completion-agent_backend.json"
        diagnostic_path = tmp_worktree / ".issue-orchestrator" / "sessions" / "run" / "invalid-completion-1.json"
        detail = {
            "failure_kind": "invalid_completion_record",
            "failure_reason": (
                "Completion record rejected: "
                "follow_up_issues exceeds maximum count (6 > 5)"
            ),
            "completion_load_failure": "invalid_schema",
            "completion_parse_error": "follow_up_issues exceeds maximum count (6 > 5)",
            "completion_path_absolute": str(completion_path),
            "diagnostic_path": str(diagnostic_path),
        }
        events = InMemoryEventSink()
        handler = make_handler(config, events=events)

        result = handler.process_completion(
            session,
            SessionStatus.FAILED,
            diagnostic_path=str(diagnostic_path),
            completion_detail=detail,
        )

        assert result.history_entry.status == "failed"
        assert "Completion record rejected" in result.history_entry.status_reason
        assert "without PR" not in result.history_entry.status_reason

        comment = next(a for a in result.actions if isinstance(a, AddCommentAction))
        assert "Completion Record Rejected" in comment.comment
        assert "did call the completion command" in comment.comment
        assert "follow_up_issues exceeds maximum count" in comment.comment
        assert "terminated without calling" not in comment.comment

        add_label = next(a for a in result.actions if isinstance(a, AddLabelAction))
        assert add_label.label == "needs-human"
        remove_label = next(a for a in result.actions if isinstance(a, RemoveLabelAction))
        assert remove_label.label == config.get_label_in_progress()

        failed_events = events.get_events(str(EventName.SESSION_FAILED))
        assert len(failed_events) >= 1
        payload = failed_events[0].data
        assert payload["failure_kind"] == "invalid_completion_record"
        assert payload["completion_load_failure"] == "invalid_schema"
        assert payload["completion_path_absolute"] == str(completion_path)
        assert payload["diagnostic_path"] == str(diagnostic_path)
        assert "follow_up_issues exceeds maximum count" in payload["error"]

    def test_invalid_json_completion_record_uses_interrupted_retry_when_enabled(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Malformed JSON is treated like interrupted output for retry policy."""
        config.retry.interrupted_sessions.enabled = True
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-456")
        repository_host = make_repository_host(
            issue_info=SimpleNamespace(labels=["agent:test"])
        )
        handler = make_handler(config, repository_host=repository_host)
        detail = {
            "failure_kind": "invalid_completion_record",
            "failure_reason": "Completion record rejected: Invalid JSON: line 1",
            "completion_load_failure": "invalid_json",
            "completion_parse_error": "Invalid JSON: line 1",
        }

        result = handler.process_completion(
            session,
            SessionStatus.FAILED,
            completion_detail=detail,
        )

        add_labels = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        comment = next(a for a in result.actions if isinstance(a, AddCommentAction))

        assert config.retry.interrupted_sessions.coding_guard_label in add_labels
        assert "needs-human" not in add_labels
        assert "Auto-retry is enabled" in comment.comment
        assert "without a valid completion record" in comment.comment

    def test_invalid_schema_completion_record_does_not_use_interrupted_retry(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Schema-invalid records surface for human repair instead of auto-retry."""
        config.retry.interrupted_sessions.enabled = True
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-456")
        repository_host = make_repository_host(
            issue_info=SimpleNamespace(labels=["agent:test"])
        )
        handler = make_handler(config, repository_host=repository_host)
        detail = {
            "failure_kind": "invalid_completion_record",
            "failure_reason": (
                "Completion record rejected: "
                "follow_up_issues exceeds maximum count (6 > 5)"
            ),
            "completion_load_failure": "invalid_schema",
            "completion_parse_error": "follow_up_issues exceeds maximum count (6 > 5)",
        }

        result = handler.process_completion(
            session,
            SessionStatus.FAILED,
            completion_detail=detail,
        )

        add_labels = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        comment = next(a for a in result.actions if isinstance(a, AddCommentAction))

        assert "needs-human" in add_labels
        assert config.retry.interrupted_sessions.coding_guard_label not in add_labels
        assert "Completion Record Rejected" in comment.comment
        assert "could not accept the completion JSON" in comment.comment

    def test_unreadable_completion_record_comment_names_read_failure(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Unreadable completion files are not described as validation rejects."""
        config.retry.interrupted_sessions.enabled = False
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-456")
        handler = make_handler(config)
        detail = {
            "failure_kind": "invalid_completion_record",
            "failure_reason": "Completion record rejected: permission denied",
            "completion_load_failure": "unreadable",
            "completion_parse_error": "permission denied",
        }

        result = handler.process_completion(
            session,
            SessionStatus.FAILED,
            completion_detail=detail,
        )

        comment = next(a for a in result.actions if isinstance(a, AddCommentAction))

        assert "could not read the completion JSON from disk" in comment.comment
        assert "rejected by orchestrator validation" not in comment.comment

    def test_review_failure_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review failure should not mark issue blocked-failed."""
        issue = make_issue(number=789)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="review-123",
            task_kind=TaskKind.REVIEW,
        )
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        actions = result.actions
        assert any(isinstance(a, AddCommentAction) for a in actions)
        assert not any(
            isinstance(a, AddLabelAction) and a.label == "blocked-failed"
            for a in actions
        )
        assert not any(isinstance(a, RemoveLabelAction) for a in actions)

    def test_failure_auto_retry_enabled_for_issue_adds_guard_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Interrupted issue session auto-retries once when enabled."""
        config.retry.interrupted_sessions.enabled = True
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-456")
        repository_host = make_repository_host(
            issue_info=SimpleNamespace(labels=["agent:test"])
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.FAILED)

        add_label = next((a for a in result.actions if isinstance(a, AddLabelAction)), None)
        comment = next((a for a in result.actions if isinstance(a, AddCommentAction)), None)
        remove_label = next((a for a in result.actions if isinstance(a, RemoveLabelAction)), None)

        assert add_label is not None
        assert add_label.label == config.retry.interrupted_sessions.coding_guard_label
        assert comment is not None
        assert "Auto-retry is enabled" in comment.comment
        assert remove_label is not None
        assert remove_label.label == config.get_label_in_progress()
        assert not any(
            isinstance(a, AddLabelAction) and a.label == "needs-human"
            for a in result.actions
        )

    def test_failure_auto_retry_guard_label_prevents_loop(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Guard label disables repeat auto-retries and falls back to blocked-needs-human."""
        config.retry.interrupted_sessions.enabled = True
        guard = config.retry.interrupted_sessions.coding_guard_label
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-456")
        repository_host = make_repository_host(
            issue_info=SimpleNamespace(labels=["agent:test", guard])
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.FAILED)

        add_labels = [a.label for a in result.actions if isinstance(a, AddLabelAction)]
        assert "needs-human" in add_labels
        assert guard not in add_labels

    def test_failure_auto_retry_enabled_for_review_adds_review_guard(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Interrupted review session auto-retries with review guard label."""
        config.retry.interrupted_sessions.enabled = True
        issue = make_issue(number=789)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="review-123",
            task_kind=TaskKind.REVIEW,
        )
        repository_host = make_repository_host(
            issue_info=SimpleNamespace(labels=["agent:test"])
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.FAILED)

        add_label = next((a for a in result.actions if isinstance(a, AddLabelAction)), None)
        comment = next((a for a in result.actions if isinstance(a, AddCommentAction)), None)

        assert add_label is not None
        assert add_label.label == config.retry.interrupted_sessions.review_guard_label
        assert comment is not None
        assert "Auto-retry is enabled" in comment.comment
        assert not any(isinstance(a, RemoveLabelAction) for a in result.actions)

    def test_completed_only_removes_in_progress_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session only removes in-progress label."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        actions = result.actions
        assert len(actions) == 1

        remove_label = actions[0]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == config.get_label_in_progress()

    def test_blocked_generates_blocked_label_and_removes_in_progress(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session adds blocked label, comments, and removes in-progress."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.BLOCKED,
            blocked_reason="Waiting for API access",
        )

        # BLOCKED adds blocked label, posts reason, and releases in-progress claim
        assert len(result.actions) == 3

        add_label = result.actions[0]
        assert isinstance(add_label, AddLabelAction)
        assert add_label.label == "blocked"

        add_comment = result.actions[1]
        assert isinstance(add_comment, AddCommentAction)
        assert "Session Blocked" in add_comment.comment
        assert "Waiting for API access" in add_comment.comment

        remove_label = result.actions[2]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == config.get_label_in_progress()

    def test_blocked_review_session_does_not_generate_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked review session does not generate label actions."""
        issue = make_issue()
        # Create a review session (terminal_id starts with "review-")
        session = create_test_session(
            issue, agent_config, tmp_worktree, terminal_id="review-1"
        )
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.BLOCKED)

        # Review sessions don't get blocking labels - they just fail silently
        assert len(result.actions) == 0

    def test_needs_human_does_not_generate_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session does not generate label actions (keeps in-progress)."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        # NEEDS_HUMAN maintains ownership via in-progress label - no actions generated
        assert len(result.actions) == 0

    def test_label_prefix_applied_to_in_progress_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Label prefix is applied to in-progress label removal."""
        config.label_prefix = "bot"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        remove_label = result.actions[0]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == "bot:in-progress"

    def test_timeout_review_session_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review timeout should not add blocking labels, only a comment."""
        issue = make_issue(number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="review-456",
            task_kind=TaskKind.REVIEW,
        )
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.TIMED_OUT)

        actions = result.actions
        # Review sessions only get a comment, no labels
        assert len(actions) == 1
        assert isinstance(actions[0], AddCommentAction)
        assert "Timed Out" in actions[0].comment
        assert "review" in actions[0].comment.lower()
        # No blocked labels for review sessions
        assert not any(isinstance(a, AddLabelAction) for a in actions)
        assert not any(isinstance(a, RemoveLabelAction) for a in actions)

    def test_timeout_rework_session_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Rework timeout should not add blocking labels, only a comment."""
        issue = make_issue(number=123)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="rework-789",
            task_kind=TaskKind.REWORK,
        )
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.TIMED_OUT)

        actions = result.actions
        assert len(actions) == 1
        assert isinstance(actions[0], AddCommentAction)
        assert "Timed Out" in actions[0].comment
        assert "rework" in actions[0].comment.lower()

    def test_blocked_with_label_prefix(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session applies label prefix to blocked label."""
        config.label_prefix = "bot"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.BLOCKED)

        # Should have 3 actions: add blocked label, comment, remove in-progress
        assert len(result.actions) == 3

        add_label = result.actions[0]
        assert isinstance(add_label, AddLabelAction)
        # Blocked label IS prefixed now (LabelManager applies prefix to all labels)
        assert add_label.label == "bot:blocked"

        add_comment = result.actions[1]
        assert isinstance(add_comment, AddCommentAction)
        assert "`bot:blocked`" in add_comment.comment

        remove_label = result.actions[2]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == "bot:in-progress"

    def test_needs_human_review_session_does_not_generate_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human review session does not generate label actions."""
        issue = make_issue()
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="review-123",
            task_kind=TaskKind.REVIEW,
        )
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        # No actions for review sessions
        assert len(result.actions) == 0


# =============================================================================
# Test: Comprehensive Status × Session Type Coverage Matrix
# =============================================================================


class TestStatusSessionTypeMatrix:
    """Comprehensive tests covering all SessionStatus × session type combinations.

    This ensures we have coverage for every permutation of:
    - SessionStatus: COMPLETED, BLOCKED, NEEDS_HUMAN, FAILED, TIMED_OUT
    - Session types: issue, review, rework

    Each test verifies:
    - Correct labels are added/removed
    - Comments are posted when expected
    - No unexpected actions are generated
    """

    # --- COMPLETED Status ---

    def test_completed_issue_session_removes_in_progress(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """COMPLETED issue session: removes in-progress, no labels added."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="issue-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.COMPLETED)

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], RemoveLabelAction)
        assert result.actions[0].label == config.get_label_in_progress()

    def test_completed_review_session_removes_in_progress(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """COMPLETED review session: removes in-progress, no labels added."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="review-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.COMPLETED)

        # Review sessions don't have in-progress to remove, but the action is still generated
        assert len(result.actions) == 1
        assert isinstance(result.actions[0], RemoveLabelAction)

    def test_completed_rework_session_removes_in_progress(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """COMPLETED rework session: removes in-progress, no labels added."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="rework-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.COMPLETED)

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], RemoveLabelAction)

    # --- BLOCKED Status ---

    def test_blocked_issue_session_adds_blocked_removes_in_progress(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """BLOCKED issue session: adds blocked label, comments, removes in-progress."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="issue-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.BLOCKED)

        assert len(result.actions) == 3
        add_label = result.actions[0]
        assert isinstance(add_label, AddLabelAction)
        assert add_label.label == "blocked"

        add_comment = result.actions[1]
        assert isinstance(add_comment, AddCommentAction)
        assert "Session Blocked" in add_comment.comment

        remove_label = result.actions[2]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == config.get_label_in_progress()

    def test_blocked_review_session_no_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """BLOCKED review session: no actions (doesn't affect issue labels)."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="review-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.BLOCKED)

        assert len(result.actions) == 0

    def test_blocked_rework_session_no_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """BLOCKED rework session: no actions (doesn't affect issue labels)."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="rework-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.BLOCKED)

        assert len(result.actions) == 0

    # --- NEEDS_HUMAN Status ---

    def test_needs_human_issue_session_no_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """NEEDS_HUMAN issue session: no actions (keeps in-progress for ownership)."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="issue-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert len(result.actions) == 0

    def test_needs_human_review_session_no_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """NEEDS_HUMAN review session: no actions."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="review-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert len(result.actions) == 0

    def test_needs_human_rework_session_no_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """NEEDS_HUMAN rework session: no actions."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="rework-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert len(result.actions) == 0

    # --- FAILED Status ---

    def test_failed_issue_session_adds_blocked_needs_human(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """FAILED issue session: adds blocked-needs-human, comment, removes in-progress."""
        config.retry.interrupted_sessions.enabled = False
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="issue-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.FAILED)

        assert len(result.actions) == 3
        add_label = next(a for a in result.actions if isinstance(a, AddLabelAction))
        assert add_label.label == "needs-human"

        comment = next(a for a in result.actions if isinstance(a, AddCommentAction))
        assert "Investigation" in comment.comment

        remove_label = next(a for a in result.actions if isinstance(a, RemoveLabelAction))
        assert remove_label.label == config.get_label_in_progress()

    def test_failed_review_session_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """FAILED review session: only adds comment, no labels."""
        config.retry.interrupted_sessions.enabled = False
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="review-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.FAILED)

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], AddCommentAction)
        assert "Investigation" in result.actions[0].comment
        assert "review" in result.actions[0].comment.lower()

    def test_failed_rework_session_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """FAILED rework session: only adds comment, no labels."""
        config.retry.interrupted_sessions.enabled = False
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="rework-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.FAILED)

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], AddCommentAction)
        assert "Investigation" in result.actions[0].comment
        assert "rework" in result.actions[0].comment.lower()

    # --- TIMED_OUT Status ---

    def test_timed_out_issue_session_adds_blocked_failed(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """TIMED_OUT issue session: adds blocked-failed, comment, removes in-progress."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="issue-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.TIMED_OUT)

        assert len(result.actions) == 3
        add_label = next(a for a in result.actions if isinstance(a, AddLabelAction))
        assert add_label.label == "blocked-failed"

        comment = next(a for a in result.actions if isinstance(a, AddCommentAction))
        assert "Timed Out" in comment.comment

        remove_label = next(a for a in result.actions if isinstance(a, RemoveLabelAction))
        assert remove_label.label == config.get_label_in_progress()

    def test_timed_out_review_session_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """TIMED_OUT review session: only adds comment, no labels."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="review-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.TIMED_OUT)

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], AddCommentAction)
        assert "Timed Out" in result.actions[0].comment
        assert "review" in result.actions[0].comment.lower()

    def test_timed_out_rework_session_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """TIMED_OUT rework session: only adds comment, no labels."""
        session = create_test_session(
            make_issue(), agent_config, tmp_worktree, terminal_id="rework-1"
        )
        result = make_handler(config).process_completion(session, SessionStatus.TIMED_OUT)

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], AddCommentAction)
        assert "Timed Out" in result.actions[0].comment
        assert "rework" in result.actions[0].comment.lower()


# =============================================================================
# Test: Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_missing_state_machines_handled_gracefully(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Handler works when state machines are not found."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        # All machine getters return None
        handler = make_handler(config)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None
        assert result.history_entry is not None

    def test_review_machine_lookup_handles_exception(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review machine lookup exceptions are caught."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        def failing_get_pr(_: int) -> None:
            raise Exception("GitHub API error")

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _: [],
            get_pr=failing_get_pr,
        )
        handler = make_handler(config, repository_host=repository_host)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None

    def test_non_review_terminal_id_patterns(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Terminal IDs that don't match review pattern are handled."""
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="some-other-pattern",
        )

        handler = make_handler(config)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None

    def test_completion_result_has_all_fields(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """CompletionResult contains all expected fields."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        # Verify all fields are present and have sensible values
        assert result.history_entry is not None
        assert result.pr_url == "http://pr"
        assert result.pr_number == 42
        assert isinstance(result.cleanup, CleanupDecision)
        assert isinstance(result.should_queue_review, bool)
        # cleanup.pending_cleanup can be None
        assert isinstance(result.actions, tuple)


# =============================================================================
# Test: Integration of Multiple Behaviors
# =============================================================================


class TestIntegrationBehaviors:
    """Tests that verify multiple behaviors work together correctly."""

    def test_successful_completion_updates_all_states_and_returns_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Successful completion updates machines, emits events, and returns actions."""
        config.code_review_agent = "agent:reviewer"
        config.tech_lead_review_agent = "agent:tech-lead"
        config.cleanup.with_tech_lead.close_ai_session_tabs = True

        events = InMemoryEventSink()
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config,
            events=events,
            repository_host=repository_host,
            issue_machine=issue_machine,
            session_machine=session_machine,
        )

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        # State machines updated
        assert issue_machine.get_state() == IssueState.PR_PENDING
        assert session_machine.get_state() == SessionState.COMPLETED

        # Events emitted
        assert events.has_event(str(EventName.SESSION_COMPLETED))
        assert events.has_event(str(EventName.PR_VIEW_CHANGED))

        # Actions generated
        assert len(result.actions) == 1
        assert isinstance(result.actions[0], RemoveLabelAction)

        # Cleanup and review decisions
        assert result.cleanup.disposition is CleanupDisposition.DEFERRED
        assert result.should_queue_review is True
        assert result.cleanup.pending_cleanup is not None

    def test_failed_session_comprehensive_behavior(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed session correctly updates state, emits events, and generates actions."""
        config.retry.interrupted_sessions.enabled = False
        events = InMemoryEventSink()
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(
            config,
            events=events,
            issue_machine=issue_machine,
            session_machine=session_machine,
        )

        result = handler.process_completion(session, SessionStatus.FAILED)

        # Session failed but issue stays in_progress (failed machine state)
        assert session_machine.get_state() == SessionState.FAILED

        # Events emitted
        assert events.has_event(str(EventName.SESSION_FAILED))

        # Actions include needs-human label (for investigation) and comment
        add_label = next((a for a in result.actions if isinstance(a, AddLabelAction)), None)
        assert add_label is not None
        assert add_label.label == "needs-human"

        # No cleanup or review queuing
        assert result.cleanup.disposition is CleanupDisposition.IMMEDIATE
        assert result.should_queue_review is False


# =============================================================================
# Test: rework_cycle Propagation in Events
# =============================================================================


class TestReworkCyclePropagation:
    """Tests that rework_cycle flows through to emitted events."""

    def test_session_completed_carries_rework_cycle(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """SESSION_COMPLETED event payload includes rework_cycle."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree,
                                      terminal_id="rework-1", task_kind=TaskKind.REWORK)
        session.rework_cycle = 2
        session.agent_label = "agent:backend"

        pr_url = "https://github.com/owner/repo/pull/42"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        events = InMemoryEventSink()
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        completed_events = events.get_events(str(EventName.SESSION_COMPLETED))
        assert len(completed_events) >= 1
        payload = completed_events[0].data
        assert payload["rework_cycle"] == 2
        assert payload["agent"] == "agent:backend"
        assert payload["task"] == "rework"

    def test_session_completed_rework_cycle_none_for_initial(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Initial coding session has rework_cycle=None."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        # Default rework_cycle is None for initial coding

        pr_url = "https://github.com/owner/repo/pull/42"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        events = InMemoryEventSink()
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        completed_events = events.get_events(str(EventName.SESSION_COMPLETED))
        assert len(completed_events) >= 1
        assert completed_events[0].data["rework_cycle"] is None

    def test_session_failed_carries_rework_cycle(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """SESSION_FAILED event payload includes rework_cycle."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        session.rework_cycle = 1

        events = InMemoryEventSink()
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.FAILED)

        failed_events = events.get_events(str(EventName.SESSION_FAILED))
        assert len(failed_events) >= 1
        assert failed_events[0].data["rework_cycle"] == 1

    def test_issue_blocked_carries_rework_cycle(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """ISSUE_BLOCKED event payload includes rework_cycle."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        session.rework_cycle = 3

        events = InMemoryEventSink()
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.BLOCKED)

        blocked_events = events.get_events(str(EventName.ISSUE_BLOCKED))
        assert len(blocked_events) >= 1
        assert blocked_events[0].data["rework_cycle"] == 3

    def test_issue_needs_human_carries_rework_cycle(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """ISSUE_NEEDS_HUMAN event payload includes rework_cycle."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        session.rework_cycle = 2

        events = InMemoryEventSink()
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        needs_events = events.get_events(str(EventName.ISSUE_NEEDS_HUMAN))
        assert len(needs_events) >= 1
        assert needs_events[0].data["rework_cycle"] == 2

    def test_issue_pr_created_carries_rework_cycle_and_agent(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """ISSUE_PR_CREATED event now includes agent, task, and rework_cycle."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        session.rework_cycle = 1
        session.agent_label = "agent:backend"

        pr_url = "https://github.com/owner/repo/pull/42"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        events = InMemoryEventSink()
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        pr_events = events.get_events(str(EventName.ISSUE_PR_CREATED))
        assert len(pr_events) >= 1
        payload = pr_events[0].data
        assert payload["rework_cycle"] == 1
        assert payload["agent"] == "agent:backend"
        assert payload["task"] == "code"


# =============================================================================
# Test: Review Outcome Event Emission
# =============================================================================


class TestReviewOutcomeEventEmission:
    """Tests that review.approved and review.changes_requested are published."""

    def test_review_approved_emitted(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """review.approved event emitted when reviewer approves."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree,
                                      terminal_id="review-42", task_kind=TaskKind.REVIEW)
        session.agent_label = "agent:reviewer"
        session.pr_number = 42

        # PR has code-reviewed label
        pr_info = SimpleNamespace(
            url="https://github.com/owner/repo/pull/42",
            number=42,
            labels=["code-reviewed"],
            draft=False,
        )
        repository_host = make_repository_host(pr_info=pr_info)

        # Set up review machine in IN_REVIEW state
        review_machine = ReviewStateMachine(pr_number=42, issue_number=1,
                                            initial_state=ReviewState.IN_REVIEW)

        events = InMemoryEventSink()
        handler = make_handler(
            config, events=events,
            repository_host=repository_host,
            review_machine=review_machine,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        approved_events = events.get_events(str(EventName.REVIEW_APPROVED))
        assert len(approved_events) == 1
        payload = approved_events[0].data
        assert payload["pr_number"] == 42
        assert payload["reviewer_agent"] == "agent:reviewer"
        assert payload["issue_number"] == 1

    def test_review_changes_requested_emitted(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """review.changes_requested event emitted when reviewer requests changes."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree,
                                      terminal_id="review-42", task_kind=TaskKind.REVIEW)
        session.agent_label = "agent:reviewer"
        session.pr_number = 42
        session.rework_cycle = 1

        # PR has needs-rework label (not code-reviewed)
        pr_info = SimpleNamespace(
            url="https://github.com/owner/repo/pull/42",
            number=42,
            labels=["needs-rework"],
            draft=False,
        )
        repository_host = make_repository_host(pr_info=pr_info)

        # Set up review machine in IN_REVIEW state
        review_machine = ReviewStateMachine(pr_number=42, issue_number=1,
                                            initial_state=ReviewState.IN_REVIEW,
                                            max_rework_cycles=5)

        events = InMemoryEventSink()
        handler = make_handler(
            config, events=events,
            repository_host=repository_host,
            review_machine=review_machine,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        cr_events = events.get_events(str(EventName.REVIEW_CHANGES_REQUESTED))
        assert len(cr_events) == 1
        payload = cr_events[0].data
        assert payload["pr_number"] == 42
        assert payload["reviewer_agent"] == "agent:reviewer"
        assert payload["rework_cycle"] == 1
        assert payload["rework_count"] == 1  # From review machine

    def test_review_outcome_not_emitted_for_non_review_session(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review outcome events not emitted for code sessions."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        pr_url = "https://github.com/owner/repo/pull/42"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        events = InMemoryEventSink()
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert events.get_events(str(EventName.REVIEW_APPROVED)) == []
        assert events.get_events(str(EventName.REVIEW_CHANGES_REQUESTED)) == []


# =============================================================================
# Test: Tech Lead decision failure transition (#6761 finding 3)
# =============================================================================


class TestTechLeadDecisionFailureTransition:
    """A rejected/missing tech_lead decision pair must fail the session for every
    flavor: FAILED history via the critical-error seam plus the blocked/failed
    labeling path for the session's own issue (ADR-0031 / #6761 finding 3)."""

    TECH_LEAD_ERROR = (
        "tech_lead_decision: missing_decision: tech_lead decision missing or empty"
    )

    def _make_tech_lead_session(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> Session:
        config.repo_root = tmp_worktree  # authority store home
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_reviewed_label = "tech-lead-reviewed"
        config.tech_lead_failed_label = "tech-lead-failed"
        issue = make_issue(labels=["agent:tech-lead"])
        return create_test_session(issue, agent_config, tmp_worktree)

    @staticmethod
    def _record_authority(config: Config, session: Session, authority: Any) -> None:
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        SqliteTechLeadAuthorityStore.for_repo(config.repo_root).record(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
            authority=authority,
        )

    def _plant_assignment(self, session: Session, assignment: Any) -> None:
        import json as _json

        from issue_orchestrator.domain.tech_lead_session import (
            TECH_LEAD_ASSIGNMENT_FILENAME,
        )

        path = session.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
        assignment.write(path)
        run_manifest_path = session.run_dir / "manifest.json"
        run_manifest = _json.loads(run_manifest_path.read_text())
        run_manifest["tech_lead_assignment"] = str(path)
        run_manifest_path.write_text(_json.dumps(run_manifest))

    def test_invalid_investigation_pair_records_failed_history_and_rejection(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        from issue_orchestrator.control.actions import SurfaceTechLeadProposalAction
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadAssignment,
            TechLeadSessionFlavor,
        )

        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority

        session = self._make_tech_lead_session(config, agent_config, tmp_worktree)
        self._plant_assignment(
            session,
            TechLeadAssignment(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                focus_issue_number=1,
                focus_reason="Investigate: timed out",
            ),
        )
        self._record_authority(
            config,
            session,
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                anchor_issue_number=session.issue.number,
                focus_issue_number=1,
            ),
        )
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.TECH_LEAD_ERROR],
        )

        assert result.history_entry.status == "failed"
        rejections = [
            a
            for a in result.actions
            if isinstance(a, SurfaceTechLeadProposalAction) and a.mode == "rejected"
        ]
        assert len(rejections) == 1
        assert rejections[0].issue_number == session.issue.number
        added = {a.label for a in result.actions if isinstance(a, AddLabelAction)}
        assert "blocked-failed" in added
        assert "publish-failed" not in added
        removed = {a.label for a in result.actions if isinstance(a, RemoveLabelAction)}
        assert "in-progress" in removed

    def test_invalid_batch_pair_records_failed_history_and_failed_labels(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        import json as _json

        from issue_orchestrator.domain.tech_lead_manifest import (
            PRToReview,
            TechLeadManifest,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadAssignment,
            TechLeadSessionFlavor,
        )

        from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority

        session = self._make_tech_lead_session(config, agent_config, tmp_worktree)
        self._plant_assignment(
            session, TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        )
        self._record_authority(
            config,
            session,
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.BATCH_REVIEW,
                anchor_issue_number=session.issue.number,
                manifest_pr_numbers=(101,),
            ),
        )
        manifest = TechLeadManifest(
            prs=[
                PRToReview(
                    number=101, title="PR 101", url="https://x/pr/101", branch="b1"
                )
            ]
        )
        manifest_path = tmp_worktree / "tech-lead-manifest.json"
        manifest.write(manifest_path)
        run_manifest_path = session.run_dir / "manifest.json"
        run_manifest = _json.loads(run_manifest_path.read_text())
        run_manifest["tech_lead_manifest"] = str(manifest_path)
        run_manifest_path.write_text(_json.dumps(run_manifest))
        handler = make_handler(config)

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.TECH_LEAD_ERROR],
        )

        assert result.history_entry.status == "failed"
        failed_labels = [
            a
            for a in result.actions
            if isinstance(a, AddLabelAction) and a.label == "tech-lead-failed"
        ]
        assert {a.issue_number for a in failed_labels} == {101}
        added = {a.label for a in result.actions if isinstance(a, AddLabelAction)}
        assert "blocked-failed" in added


# =============================================================================
# Test: Tech Lead authority retention at the completion finalization seam
# =============================================================================


class TestTechLeadAuthorityRetention:
    """Completion finalization is a run's terminal seam: the recorded launch
    authority row must be discarded once the completion is processed, for
    success and failure alike (#6769 finding 3)."""

    # Reuse the arm/plant helpers without inheriting (and re-collecting)
    # the transition tests themselves.
    _helpers = TestTechLeadDecisionFailureTransition()
    _make_tech_lead_session = _helpers._make_tech_lead_session
    _plant_assignment = _helpers._plant_assignment
    _record_authority = staticmethod(
        TestTechLeadDecisionFailureTransition._record_authority
    )
    TECH_LEAD_ERROR = TestTechLeadDecisionFailureTransition.TECH_LEAD_ERROR

    def _load_authority(self, config: Config, session: Session) -> Any:
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )

        return SqliteTechLeadAuthorityStore.for_repo(config.repo_root).load(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

    def _armed_investigation(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> Session:
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadAssignment,
            TechLeadLaunchAuthority,
            TechLeadSessionFlavor,
        )

        session = self._make_tech_lead_session(config, agent_config, tmp_worktree)
        self._plant_assignment(
            session,
            TechLeadAssignment(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                focus_issue_number=1,
                focus_reason="Investigate: timed out",
            ),
        )
        self._record_authority(
            config,
            session,
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                anchor_issue_number=session.issue.number,
                focus_issue_number=1,
            ),
        )
        return session

    def test_rejected_completion_discards_authority_row(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        session = self._armed_investigation(config, agent_config, tmp_worktree)
        assert self._load_authority(config, session) is not None
        handler = make_handler(config)

        handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.TECH_LEAD_ERROR],
        )

        assert self._load_authority(config, session) is None

    def test_failed_session_discards_authority_row(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        session = self._armed_investigation(config, agent_config, tmp_worktree)
        handler = make_handler(config)

        handler.process_completion(session, SessionStatus.FAILED)

        assert self._load_authority(config, session) is None

    def test_publish_stage_failure_keeps_authority_row_for_retry(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Retry Publish re-enters CompletionProcessor.process for this same
        run and re-validates the launch authority — a publish-stage failure
        must NOT discard the row (PublishRecoveryService owns that terminal)."""
        session = self._armed_investigation(config, agent_config, tmp_worktree)
        handler = make_handler(config)

        handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=["push_branch: Push failed: remote rejected"],
        )

        assert self._load_authority(config, session) is not None
