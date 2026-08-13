"""Unit tests for SessionLauncher claim integration."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from issue_orchestrator.control.completion_handler import CleanupDecision
from issue_orchestrator.domain.claim import ClaimResult, ClaimState
from issue_orchestrator.domain.coder_prompt import PreparedCoderPromptAddendum
from issue_orchestrator.domain.lease_config import LeaseConfig
from issue_orchestrator.domain.models import Session, SessionStatus
from tests.callback_endpoint_helpers import ready_callback_endpoint
from issue_orchestrator.ports import NullBoardSnapshotProvider
from tests.unit.session_run_helpers import make_session_run_assets


class MockClaimManager:
    """Mock ClaimManager for testing."""

    def __init__(self):
        self.attempt_claim_calls: list[int] = []
        self.run_convergence_calls: list[tuple[int, str]] = []
        self.release_claim_calls: list[tuple[int, str]] = []
        self.claim_result = ClaimResult(
            success=True,
            lease_id="test-lease-123",
            state=ClaimState.CLAIMING,
        )
        self.convergence_result = True

    def configure_claim_failure(self, error: str = "Claim failed"):
        """Configure claim to fail."""
        self.claim_result = ClaimResult.failed(error)

    def configure_convergence_failure(self):
        """Configure convergence to fail."""
        self.convergence_result = False

    def attempt_claim(self, issue_number: int) -> ClaimResult:
        self.attempt_claim_calls.append(issue_number)
        return self.claim_result

    def run_convergence(self, issue_number: int, lease_id: str) -> bool:
        self.run_convergence_calls.append((issue_number, lease_id))
        return self.convergence_result

    def release_claim(self, issue_number: int, lease_id: str) -> None:
        self.release_claim_calls.append((issue_number, lease_id))

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        return True

    def get_current_claim(self, issue_number: int):
        return None


class MockEventSink:
    """Mock event sink for testing."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, event):
        self.events.append((event.event_type, event.data))


class MockIssue:
    """Mock issue for testing."""

    def __init__(self, number: int = 42, title: str = "Test Issue"):
        self.number = number
        self.title = title
        self.agent_type = "test-agent"
        self.labels = ["test-agent"]
        self.body = "Test issue body"
        self.key = MagicMock()
        self.key.stable_id.return_value = f"issue-{number}"


class TestSessionLauncherClaimAcquisition:
    """Tests for claim acquisition in SessionLauncher.launch_issue_session."""

    @pytest.fixture
    def mock_claim_manager(self):
        return MockClaimManager()

    @pytest.fixture
    def mock_events(self):
        return MockEventSink()

    def test_claim_expiry_uses_nested_claims_lease_seconds(
        self, mock_claim_manager, mock_events
    ):
        """Session-local claim expiry honors claims.lease_seconds."""
        from issue_orchestrator.control.session_launcher import SessionLauncher
        from issue_orchestrator.infra.config import Config

        config = Config()
        config.claims.lease_seconds = 30

        launcher = SessionLauncher(
            config=config,
            events=mock_events,
            repository_host=MagicMock(),
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=MagicMock(),
            working_copy=MagicMock(),
            command_runner=MagicMock(),
            session_output=MagicMock(),
            manifest_downloader=MagicMock(),
            tech_lead_authority=MagicMock(),
            session_exists_fn=lambda name: False,
            create_session_fn=lambda *args: True,
            get_issue_machine=lambda issue: MagicMock(state="AVAILABLE"),
            get_session_machine=lambda *args: MagicMock(),
            get_review_machine=lambda *args: MagicMock(),
            claim_manager=mock_claim_manager,
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=ready_callback_endpoint(),
        )

        claim = launcher._acquire_issue_claim(MockIssue())  # noqa: SLF001

        assert claim.success is True
        assert claim.lease_acquired_at is not None
        assert claim.lease_expires_at is not None
        assert (
            claim.lease_expires_at - claim.lease_acquired_at
        ).total_seconds() == 30

    def test_launch_prepares_prompt_before_acquiring_claim(
        self, mock_claim_manager, mock_events
    ):
        """Required prompt input is resolved before the claim mutates GitHub."""
        # Track order of operations
        operations = []

        def track_claim(issue_number):
            operations.append(("claim", issue_number))
            return mock_claim_manager.claim_result

        def track_convergence(issue_number, lease_id):
            operations.append(("convergence", issue_number, lease_id))
            return mock_claim_manager.convergence_result

        mock_claim_manager.attempt_claim = track_claim
        mock_claim_manager.run_convergence = track_convergence

        # Create worktree context that tracks when it's called
        with patch(
            "issue_orchestrator.control.session_launcher.WorktreeContext"
        ) as mock_ctx_class:
            mock_ctx = MagicMock()
            mock_ctx.error = None
            mock_ctx.worktree_path = Path("/tmp/worktree")
            mock_ctx.branch_name = "test-branch"
            mock_ctx.worktree_info = MagicMock(rebase_failed=False)
            mock_ctx.run = make_session_run_assets(
                Path("/tmp/worktree"),
                run_id="test-run",
                session_name="issue-42",
            )
            mock_ctx.claude_project_dir = Path("/tmp/claude")

            def create_worktree(*args, **kwargs):
                operations.append(("worktree", kwargs.get("issue_number")))
                return mock_ctx

            mock_ctx_class.create = create_worktree

            # Create minimal SessionLauncher with mocks
            with patch.multiple(
                "issue_orchestrator.control.session_launcher",
                detect_existing_work=lambda *args, **kwargs: None,
                get_completion_path=lambda *args, **kwargs: "completion.json",
            ):
                from issue_orchestrator.control.session_launcher import SessionLauncher
                from issue_orchestrator.infra.config import Config

                mock_config = MagicMock(spec=Config)
                mock_config.claims = MagicMock(lease_seconds=900)
                mock_config.repo = "test/repo"
                mock_config.agents = {"test-agent": MagicMock(
                    timeout_minutes=30,
                    get_command=lambda **kwargs: "test-command",
                    get_command_for_prompt=lambda *args, **kwargs: "test-command",
                    provider=None,
                )}
                mock_config.setup_worktree = []
                mock_config.get_label_in_progress.return_value = "in-progress"
                mock_config.enforce_hooks = False
                mock_config.pre_push_hook = None
                mock_config.reuse_push_preflight = False
                mock_config.worktree_branch_on_recreate = "recreate"
                mock_config.allow_no_verify_dry_run_preflight = False
                mock_config.web_port = 8080
                mock_config.e2e_pr_labels = []
                mock_config.provider_resilience = MagicMock(short_retry=MagicMock(
                    max_attempts=1,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=1,
                    jitter=False,
                ))
                mock_config.retry = MagicMock(interrupted_sessions=MagicMock(
                    coding_guard_label="io:auto-retried-interrupted-coding",
                    review_guard_label="io:auto-retried-interrupted-review",
                ))

                prompt_provider = MagicMock()

                def prepare_prompt(*args, **kwargs):
                    operations.append(("prompt", kwargs["agent_label"]))
                    return PreparedCoderPromptAddendum(None)

                prompt_provider.prepare.side_effect = prepare_prompt

                launcher = SessionLauncher(
                    config=mock_config,
                    events=mock_events,
                    repository_host=MagicMock(),
                    action_applier=MagicMock(apply=lambda a: MagicMock(success=True)),
                    session_manager=MagicMock(),
                    worktree_manager=MagicMock(),
                    working_copy=MagicMock(),
                    command_runner=MagicMock(),
                    session_output=MagicMock(),
                    manifest_downloader=MagicMock(),
                    tech_lead_authority=MagicMock(),
                    session_exists_fn=lambda name: False,
                    create_session_fn=lambda *args: True,
                    get_issue_machine=lambda issue: MagicMock(state="AVAILABLE"),
                    get_session_machine=lambda *args: MagicMock(),
                    get_review_machine=lambda *args: MagicMock(),
                    claim_manager=mock_claim_manager,
                    board_snapshot_provider=NullBoardSnapshotProvider(),
                    agent_callback_endpoint=ready_callback_endpoint(),
                    coder_prompt_addendum=prompt_provider,
                )

                issue = MockIssue()
                result = launcher.launch_issue_session(issue, [])

                # Trusted prompt input is read first; the claim then protects
                # every mutation performed by worktree creation/reset.
                claim_idx = next(
                    (i for i, op in enumerate(operations) if op[0] == "claim"), -1
                )
                worktree_idx = next(
                    (i for i, op in enumerate(operations) if op[0] == "worktree"), -1
                )

                assert claim_idx >= 0, "Claim should have been attempted"
                assert worktree_idx >= 0, "Worktree should have been created"
                prompt_idx = next(
                    (i for i, op in enumerate(operations) if op[0] == "prompt"), -1
                )
                assert prompt_idx >= 0, "Prompt input should have been prepared"
                assert prompt_idx < claim_idx < worktree_idx

    def test_launch_fails_if_claim_fails(self, mock_claim_manager, mock_events):
        """Launch fails if claim acquisition fails."""
        mock_claim_manager.configure_claim_failure("Network error")

        from issue_orchestrator.control.session_launcher import SessionLauncher
        from issue_orchestrator.infra.config import Config

        mock_config = MagicMock(spec=Config)
        mock_config.claims = MagicMock(lease_seconds=900)
        mock_config.repo = "test/repo"
        mock_config.agents = {"test-agent": MagicMock(provider=None)}
        mock_config.provider_resilience = MagicMock(short_retry=MagicMock(
            max_attempts=1,
            initial_backoff_seconds=1,
            max_backoff_seconds=1,
            jitter=False,
        ))

        worktree_manager = MagicMock()
        launcher = SessionLauncher(
            config=mock_config,
            events=mock_events,
            repository_host=MagicMock(),
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=worktree_manager,
            working_copy=MagicMock(),
            command_runner=MagicMock(),
            session_output=MagicMock(),
            manifest_downloader=MagicMock(),
            tech_lead_authority=MagicMock(),
            session_exists_fn=lambda name: False,
            create_session_fn=lambda *args: True,
            get_issue_machine=lambda issue: MagicMock(state="AVAILABLE"),
            get_session_machine=lambda *args: MagicMock(),
            get_review_machine=lambda *args: MagicMock(),
            claim_manager=mock_claim_manager,
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=ready_callback_endpoint(),
        )

        issue = MockIssue()
        result = launcher.launch_issue_session(issue, [])

        assert result.success is False
        assert "claim" in result.reason.lower()
        worktree_manager.create.assert_not_called()

    def test_launch_releases_claim_on_convergence_failure(
        self, mock_claim_manager, mock_events
    ):
        """Claim is released if convergence fails."""
        mock_claim_manager.configure_convergence_failure()

        from issue_orchestrator.control.session_launcher import SessionLauncher
        from issue_orchestrator.infra.config import Config

        mock_config = MagicMock(spec=Config)
        mock_config.claims = MagicMock(lease_seconds=900)
        mock_config.repo = "test/repo"
        mock_config.agents = {"test-agent": MagicMock(provider=None)}
        mock_config.provider_resilience = MagicMock(short_retry=MagicMock(
            max_attempts=1,
            initial_backoff_seconds=1,
            max_backoff_seconds=1,
            jitter=False,
        ))

        worktree_manager = MagicMock()
        launcher = SessionLauncher(
            config=mock_config,
            events=mock_events,
            repository_host=MagicMock(),
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=worktree_manager,
            working_copy=MagicMock(),
            command_runner=MagicMock(),
            session_output=MagicMock(),
            manifest_downloader=MagicMock(),
            tech_lead_authority=MagicMock(),
            session_exists_fn=lambda name: False,
            create_session_fn=lambda *args: True,
            get_issue_machine=lambda issue: MagicMock(state="AVAILABLE"),
            get_session_machine=lambda *args: MagicMock(),
            get_review_machine=lambda *args: MagicMock(),
            claim_manager=mock_claim_manager,
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=ready_callback_endpoint(),
        )

        issue = MockIssue()
        result = launcher.launch_issue_session(issue, [])

        assert result.success is False
        assert "convergence" in result.reason.lower()
        # Verify claim was released
        assert len(mock_claim_manager.release_claim_calls) == 1
        assert mock_claim_manager.release_claim_calls[0][0] == issue.number
        worktree_manager.create.assert_not_called()


class TestSessionCompletionClaimRelease:
    """Tests for claim release on session completion."""

    @pytest.fixture
    def mock_claim_manager(self):
        return MockClaimManager()

    @pytest.fixture
    def mock_events(self):
        return MockEventSink()

    def test_completion_releases_claim(self, mock_claim_manager, mock_events):
        """Session completion releases the claim."""
        from issue_orchestrator.control.session_completion import handle_session_completion
        from issue_orchestrator.domain.models import Issue, Session, SessionKey, TaskKind

        # Create a session with a lease_id
        issue_key = MagicMock()
        issue_key.stable_id.return_value = "issue-42"
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

        issue = Issue(number=42, title="Test Issue", labels=["test-agent"])

        session = Session(
            key=session_key,
            issue=issue,
            agent_config=MagicMock(command="test"),
            terminal_id="issue-42",
            worktree_path=Path("/tmp/worktree"),
            branch_name="test-branch",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="issue-42",
            ),
            completion_path="completion.json",
            agent_label="test-agent",
            lease_id="test-lease-123",
            lease_acquired_at=datetime.now() - timedelta(minutes=5),
            lease_expires_at=datetime.now() + timedelta(minutes=10),
        )

        mock_state = MagicMock()
        mock_state.active_sessions = [session]
        mock_state.session_history = []
        mock_state.pending_cleanups = []
        mock_state.immediate_cleanups = []
        mock_state.discovered_reviews = []
        mock_state.discovered_failures = []
        mock_state.failed_this_cycle = set()
        mock_state.completed_today = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=(),
            history_entry=MagicMock(),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
        )

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=mock_state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(apply_all=lambda x: None),
            observer=MagicMock(handle_completion=lambda *args: None),
            worktree_manager=None,
            kill_session_fn=lambda name: None,
            config=MagicMock(validation_cmd=None),
            session_output=MagicMock(),
            claim_manager=mock_claim_manager,
            events=mock_events,
            pending_work_claims=_test_claim_store(),
        )

        # Verify claim was released
        assert len(mock_claim_manager.release_claim_calls) == 1
        issue_num, lease_id = mock_claim_manager.release_claim_calls[0]
        assert issue_num == 42
        assert lease_id == "test-lease-123"

    def test_completion_skips_release_without_lease(
        self, mock_claim_manager, mock_events
    ):
        """Session without lease_id doesn't try to release."""
        from issue_orchestrator.control.session_completion import handle_session_completion
        from issue_orchestrator.domain.models import Issue, Session, SessionKey, TaskKind

        # Create session WITHOUT lease_id
        issue_key = MagicMock()
        issue_key.stable_id.return_value = "issue-42"
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

        issue = Issue(number=42, title="Test Issue", labels=["test-agent"])

        session = Session(
            key=session_key,
            issue=issue,
            agent_config=MagicMock(command="test"),
            terminal_id="issue-42",
            worktree_path=Path("/tmp/worktree"),
            branch_name="test-branch",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="issue-42",
            ),
            completion_path="completion.json",
            agent_label="test-agent",
            # No lease_id
        )

        mock_state = MagicMock()
        mock_state.active_sessions = [session]
        mock_state.session_history = []
        mock_state.pending_cleanups = []
        mock_state.immediate_cleanups = []
        mock_state.discovered_reviews = []
        mock_state.discovered_failures = []
        mock_state.failed_this_cycle = set()
        mock_state.completed_today = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=(),
            history_entry=MagicMock(),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
        )

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=mock_state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(apply_all=lambda x: None),
            observer=MagicMock(handle_completion=lambda *args: None),
            worktree_manager=None,
            kill_session_fn=lambda name: None,
            config=MagicMock(validation_cmd=None),
            session_output=MagicMock(),
            claim_manager=mock_claim_manager,
            events=mock_events,
            pending_work_claims=_test_claim_store(),
        )

        # Verify no release was attempted
        assert len(mock_claim_manager.release_claim_calls) == 0


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
