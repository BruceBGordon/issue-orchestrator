"""Unit tests for WorktreeContext.

Tests for the WorktreeContext dataclass and helper functions.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from issue_orchestrator.control.worktree_context import (
    WorktreeContext,
    _escape_claude_project_path,
)
from issue_orchestrator.control.worktree import WorktreePreparationError
from issue_orchestrator.ports.worktree_manager import WorktreeInfo, WorktreeReuseOptions



class _NoSetupRunner:
    """CommandRunner stub for contexts whose config declares no setup commands.

    Acquisition provisions the worktree environment now, so every context needs
    a runner. These tests configure no setup commands, so it is never invoked;
    a test that DOES configure them asserts on the commands instead.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command, **kwargs):
        from types import SimpleNamespace

        self.commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="", timed_out=False)


class TestEscapeClaudeProjectPath:
    """Tests for the _escape_claude_project_path function."""

    def test_escapes_simple_path(self):
        """Verify simple path escaping."""
        path = Path("/Users/test/project")
        result = _escape_claude_project_path(path)
        assert result == "-Users-test-project"

    def test_escapes_path_with_multiple_segments(self):
        """Verify path with many segments."""
        path = Path("/a/b/c/d/e")
        result = _escape_claude_project_path(path)
        assert result == "-a-b-c-d-e"

    def test_handles_root_path(self):
        """Verify root path handling."""
        path = Path("/")
        result = _escape_claude_project_path(path)
        assert result == "-"


class TestWorktreeContextCreate:
    """Tests for WorktreeContext.create factory method."""

    @pytest.fixture
    def mock_worktree_manager(self, tmp_path):
        """Create a mock worktree manager."""
        manager = MagicMock()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        manager.create.return_value = WorktreeInfo(
            path=worktree_path,
            branch_name="issue-123-fix-bug",
            reuse_status="created",
            reuse_reason="new worktree",
            uncommitted_discarded=0,
            commits_discarded=0,
            rebase_failed=False,
        )
        return manager

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create a mock config."""
        config = MagicMock()
        config.repo_root = tmp_path
        config.worktree_base = None
        config.worktree_base_branch_override = None
        config.session_output_retention_runs = 5
        config.session_output_retention_days = 7
        config.session_output_retention_tier = "hot"
        config.terminal_adapter = "subprocess"
        return config

    @pytest.fixture
    def mock_events(self):
        """Create a mock event sink."""
        return MagicMock()

    @pytest.fixture
    def mock_session_output(self, tmp_path):
        """Create a mock session output."""
        session_output = MagicMock()
        mock_run = MagicMock()
        mock_run.run_id = "run-123"
        mock_run.run_dir = tmp_path / "runs" / "run-123"
        session_output.start_run.return_value = mock_run
        return session_output

    def test_creates_context_successfully(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output, tmp_path
    ):
        """Verify WorktreeContext.create returns a valid context."""
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree = MagicMock()
            mock_worktree_cls.return_value = mock_worktree

            ctx = WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
            )

            assert ctx.error is None
            assert ctx.issue_number == 123
            assert ctx.session_name == "issue-123"
            assert ctx.branch_name == "issue-123-fix-bug"
            mock_worktree.prepare_for_session.assert_called_once_with("issue-123")

    def test_returns_error_on_worktree_preparation_failure(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """Verify WorktreeContext.create captures preparation errors."""
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree = MagicMock()
            # Get worktree path from the fixture (already created)
            worktree_path = mock_worktree_manager.create.return_value.path
            prep_error = WorktreePreparationError(
                path=worktree_path,
                issue_number=123,
                message="Could not delete stale files",
            )
            mock_worktree.prepare_for_session.side_effect = prep_error
            mock_worktree_cls.return_value = mock_worktree

            ctx = WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
            )

            assert ctx.error is prep_error
            assert ctx.issue_number == 123

    def test_returns_error_on_worktree_creation_failure(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output, tmp_path
    ):
        """Verify WorktreeContext.create captures worktree create failures."""
        mock_worktree_manager.create.side_effect = RuntimeError("branch already used by worktree")
        mock_config.worktree_base = tmp_path

        ctx = WorktreeContext.create(
            command_runner=_NoSetupRunner(),
            worktree_manager=mock_worktree_manager,
            config=mock_config,
            events=mock_events,
            session_output=mock_session_output,
            issue_number=123,
            issue_title="Fix bug",
            session_name="issue-123",
            agent_label="agent:developer",
        )

        assert isinstance(ctx.error, WorktreePreparationError)
        assert ctx.error is not None
        assert "Cannot create worktree" in str(ctx.error)
        assert ctx.run is None
        assert ctx.worktree_info.reuse_reason == "worktree_create_failed"

    def test_emits_worktree_reset_event_when_work_discarded(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output, tmp_path
    ):
        """Verify event is emitted when commits are discarded during reset."""
        # Get the worktree path already created by fixture
        worktree_path = mock_worktree_manager.create.return_value.path
        # Update return value to include discarded work
        mock_worktree_manager.create.return_value = WorktreeInfo(
            path=worktree_path,
            branch_name="issue-123-fix-bug",
            reuse_status="reused",
            reuse_reason="existing worktree reset",
            uncommitted_discarded=2,
            commits_discarded=3,
            rebase_failed=False,
        )

        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree = MagicMock()
            mock_worktree_cls.return_value = mock_worktree

            WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
            )

            # Verify event was emitted
            mock_events.publish.assert_called()
            call_args = mock_events.publish.call_args[0][0]
            assert call_args.data["uncommitted_discarded"] == 2
            assert call_args.data["commits_discarded"] == 3

    def test_phase_name_defaults_to_session_name(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """Verify phase_name defaults to session_name when not provided."""
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree = MagicMock()
            mock_worktree_cls.return_value = mock_worktree

            ctx = WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
            )

            assert ctx.session_name == "issue-123"
            assert ctx.phase_name == "issue-123"  # Defaults to session_name
            # Verify prepare_for_session uses phase_name
            mock_worktree.prepare_for_session.assert_called_once_with("issue-123")

    def test_phase_name_can_be_set_independently(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """Verify phase_name can be set separately from session_name."""
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree = MagicMock()
            mock_worktree_cls.return_value = mock_worktree

            ctx = WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
                phase_name="coding-1",
            )

            assert ctx.session_name == "issue-123"
            assert ctx.phase_name == "coding-1"
            # Verify prepare_for_session uses phase_name, not session_name
            mock_worktree.prepare_for_session.assert_called_once_with("coding-1")

    def test_stack_base_branch_seeds_worktree_from_predecessor(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """A stack successor seeds from its predecessor branch (#6596).

        The predecessor branch is passed as the worktree base and any configured
        seed ref is suppressed so a freshly created successor branch is built
        from the predecessor head (and the publish ancestry gate is satisfied
        without manual rebasing).
        """
        mock_config.worktree_base_branch_override = "main"
        mock_config.worktree_seed_ref = "seed-abc"
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree_cls.return_value = MagicMock()

            WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
                stack_base_branch="20-base",
            )

            call_kwargs = mock_worktree_manager.create.call_args.kwargs
            assert call_kwargs["base_branch"] == "20-base"
            assert call_kwargs["seed_ref"] is None

    def test_scratch_identity_overrides_name_branch_and_suppresses_seed(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """A scratch investigation OWNS its worktree name/branch and is a clean
        checkout off the base branch (#6823): the configured seed ref and any
        stack base are suppressed so it never seeds from the subject's branch."""
        from issue_orchestrator.control.worktree_context import (
            ScratchWorktreeIdentity,
        )

        mock_config.worktree_base_branch_override = "main"
        mock_config.worktree_seed_ref = "seed-abc"
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree_cls.return_value = MagicMock()

            WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=5980,
                issue_title="Investigate failure",
                session_name="issue-5980",
                agent_label="agent:tech-lead",
                stack_base_branch="20-base",  # ignored for a scratch investigation
                scratch=ScratchWorktreeIdentity(
                    worktree_name="repo-tech-lead-5980-tok",
                    branch_name="tech-lead-investigation-5980-tok",
                ),
            )

            call_kwargs = mock_worktree_manager.create.call_args.kwargs
            assert call_kwargs["worktree_name"] == "repo-tech-lead-5980-tok"
            assert call_kwargs["branch_name"] == "tech-lead-investigation-5980-tok"
            assert call_kwargs["base_branch"] == "main"
            assert call_kwargs["seed_ref"] is None

    def test_no_stack_base_uses_configured_default(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """A non-stack issue keeps the configured base and seed ref unchanged."""
        mock_config.worktree_base_branch_override = "main"
        mock_config.worktree_seed_ref = "seed-abc"
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree_cls.return_value = MagicMock()

            WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
            )

            call_kwargs = mock_worktree_manager.create.call_args.kwargs
            assert call_kwargs["base_branch"] == "main"
            assert call_kwargs["seed_ref"] == "seed-abc"

    def test_session_output_uses_phase_name_for_run(
        self, mock_worktree_manager, mock_config, mock_events, mock_session_output
    ):
        """Verify session_output.start_run receives phase_name, not session_name."""
        with patch(
            "issue_orchestrator.control.worktree_context.Worktree"
        ) as mock_worktree_cls:
            mock_worktree = MagicMock()
            mock_worktree_cls.return_value = mock_worktree

            WorktreeContext.create(
                command_runner=_NoSetupRunner(),
                worktree_manager=mock_worktree_manager,
                config=mock_config,
                events=mock_events,
                session_output=mock_session_output,
                issue_number=123,
                issue_title="Fix bug",
                session_name="issue-123",
                agent_label="agent:developer",
                phase_name="review-2",
            )

            # Verify start_run was called with phase_name
            mock_session_output.start_run.assert_called_once()
            call_kwargs = mock_session_output.start_run.call_args[1]
            assert call_kwargs["session_name"] == "review-2"
