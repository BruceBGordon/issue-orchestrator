"""Integration tests that verify component wiring.

These tests mock only at the subprocess boundary (git, tmux commands)
and let the internal Python code actually run. This catches wiring bugs
that unit tests miss when they mock everything.

Note: We still inject mock adapters (like MockGitHubAdapter) to avoid real
API calls, but this tests the actual wiring between components.
"""

import asyncio
import argparse
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from tempfile import TemporaryDirectory

from issue_orchestrator.infra.config import Config, DangerousConfig
from issue_orchestrator.domain.models import (
    Issue, AgentConfig, Session, OrchestratorState, SessionStatus,
    CommentHeadings
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets
# Import MockGitHubAdapter from conftest (it's available as fixture)


class TestOrchestratorWiring:
    """Test that Orchestrator methods are properly wired together."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary git repository."""
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=repo,
                check=True,
            )
            yield repo

    @pytest.fixture
    def config(self, temp_repo):
        """Create a minimal config."""
        config = Config()
        config.repo = "owner/repo"
        config.repo_root = temp_repo
        config.worktree_base = temp_repo / "worktrees"
        config.ui_mode = "tmux"  # Use tmux so tests can patch create_session
        config.agents = {
            "agent:test": AgentConfig(
                prompt_path=Path("test.md"),
                timeout_minutes=5,
            )
        }
        config.max_concurrent_sessions = 2
        # Use temp directory for state file to isolate tests
        config.state_file = temp_repo / ".issue-orchestrator" / "state.json"
        # Tests are not exercising hook enforcement.
        config.dangerous = DangerousConfig(allow_unsupported_agents=True)
        return config

    @pytest.mark.asyncio
    async def test_startup_queries_in_progress_issues(self, config, mock_repository_host):
        """Verify startup() queries for in-progress issues."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
        from tests.conftest import build_test_orchestrator_deps, MockEventSink, MockSessionRunner

        working_copy = MagicMock()
        working_copy.list_remote_branches.return_value = []

        events = MockEventSink()
        runner = MockSessionRunner()
        worktree_manager = GitWorktreeManager()

        deps = build_test_orchestrator_deps(
            config, mock_repository_host, events, runner, worktree_manager, working_copy=working_copy
        )

        orchestrator = Orchestrator(config=config, deps=deps)

        await orchestrator.startup()

        # Verify list_issues was called via the adapter
        assert len(mock_repository_host.list_issues_calls) > 0

    def test_orchestrator_wires_action_applier_claim_guard(
        self,
        config,
        mock_repository_host,
        patch_plugin_manager,
    ):
        """Orchestrator initialization should connect claim enforcement to ActionApplier."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from tests.conftest import build_test_orchestrator_deps, MockEventSink

        events = MockEventSink()
        claim_manager = MagicMock()
        deps = build_test_orchestrator_deps(
            config,
            mock_repository_host,
            events,
            patch_plugin_manager,
            MagicMock(),
            claim_manager=claim_manager,
        )

        orchestrator = Orchestrator(config=config, deps=deps)
        session = MagicMock()
        session.issue.number = 456
        session.lease_id = "lease-456"
        orchestrator.state.active_sessions.append(session)

        assert orchestrator.deps.action_applier.claim_gate is orchestrator.deps.claim_gate
        assert orchestrator.deps.action_applier.lease_id_lookup(456) == "lease-456"

    def test_launch_session_creates_worktree_and_window(
        self,
        config,
        temp_repo,
        patch_plugin_manager,
        mock_repository_host,
    ):
        """Verify launch_session actually creates worktree and tmux window."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from issue_orchestrator.ports.worktree_manager import WorktreeInfo
        from tests.conftest import build_test_orchestrator_deps, MockEventSink
        # Configure mock plugin to allow session creation
        patch_plugin_manager.plugin.session_exists_override = False

        # Create a mock WorktreeManager
        mock_worktree_manager = MagicMock()
        worktree_path = temp_repo / "worktrees" / "issue-456"
        worktree_path.mkdir(parents=True, exist_ok=True)
        mock_worktree_manager.create.return_value = WorktreeInfo(
            path=worktree_path,
            branch_name="456-test-feature",
        )

        working_copy = MagicMock()
        working_copy.list_remote_branches.return_value = []

        events = MockEventSink()

        deps = build_test_orchestrator_deps(
            config, mock_repository_host, events, patch_plugin_manager, mock_worktree_manager, working_copy=working_copy
        )

        orchestrator = Orchestrator(config=config, deps=deps)
        test_issue = Issue(
            number=456,
            title="Test Feature",
            labels=["agent:test"],  # Must match config's agent key
            state="open"
        )

        # launch_session only takes issue - gets agent_config internally
        session = orchestrator.launch_session(test_issue)

        # Verify all steps happened
        assert mock_worktree_manager.create.called, "Worktree should be created"
        # Verify window created via plugin manager
        assert len(patch_plugin_manager.plugin.create_session_calls) == 1, "Tmux window should be created"
        # Verify label added via the mock adapter
        assert (456, "in-progress") in mock_repository_host.add_label_calls, "In-progress label should be added"
        assert session is not None
        assert session.issue.number == 456


class TestCLIWiring:
    """Test that CLI commands are wired to correct orchestrator methods."""

    def test_cmd_start_loads_config(self):
        """Verify cmd_start loads config correctly.

        Note: cmd_start has lazy imports and uses asyncio.run(),
        so we test at a simpler level.
        """
        from issue_orchestrator.entrypoints.cli import cmd_start

        # Patch at config module level since it's imported inside cmd_start
        with patch('issue_orchestrator.infra.config.Config.find_and_load') as mock_config:
            mock_cfg = MagicMock()
            mock_cfg.agents = {"agent:test": MagicMock()}
            mock_cfg.max_concurrent_sessions = 2
            mock_cfg.repo_root = Path("/fake")
            mock_config.return_value = mock_cfg

            # Patch orchestrator module since it's imported inside cmd_start
            with patch('issue_orchestrator.infra.orchestrator.Orchestrator') as mock_orch_class:
                mock_orch = MagicMock()
                mock_orch.startup = AsyncMock()
                mock_orch.run_loop = AsyncMock()
                mock_orch.shutdown_requested = False
                mock_orch_class.return_value = mock_orch

                # Patch dashboard module
                with patch('issue_orchestrator.entrypoints.dashboard.run_with_dashboard', new_callable=AsyncMock):
                    with patch('asyncio.run') as mock_asyncio_run:
                        mock_asyncio_run.return_value = None

                        args = argparse.Namespace(
                            no_dashboard=False,
                            dry_run=False,
                            test_mode=False
                        )

                        cmd_start(args)

                        # Verify config was loaded
                        mock_config.assert_called_once()

    def test_cmd_status_returns_without_error(self):
        """Verify cmd_status can execute without error."""
        from issue_orchestrator.entrypoints.cli import cmd_status

        with patch('issue_orchestrator.infra.config.Config.find_and_load') as mock_config:
            mock_cfg = MagicMock()
            mock_cfg.agents = {"agent:test": MagicMock()}
            mock_cfg.repo_root = Path("/fake")
            mock_cfg.repo = None
            mock_cfg.filtering = MagicMock()
            mock_cfg.filtering.label = None
            mock_cfg.filtering.milestones = []
            mock_cfg.filtering.milestone = None
            mock_cfg.max_concurrent_sessions = 3
            mock_config.return_value = mock_cfg

            args = argparse.Namespace()
            result = cmd_status(args)

            # Should complete without error
            assert result == 0 or result is None


class TestCommentHeadingsWiring:
    """Test that comment headings are properly loaded and available."""

    def test_config_loads_comment_headings(self, tmp_path):
        """Verify comment headings are loaded from YAML."""
        config_content = """
worktrees:
  base: "../"

agents:
  "agent:test":
    prompt: "test.md"

observability:
  comment_headings:
    implementation: "## Done"
    problems: "## Issues"
    pr_link: "## PR"
    blocked: "## Stuck"
    needs_human: "## Help Needed"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Verify headings were loaded
        assert config.comment_headings.implementation == "## Done"
        assert config.comment_headings.problems == "## Issues"
        assert config.comment_headings.pr_link == "## PR"
        assert config.comment_headings.blocked == "## Stuck"
        assert config.comment_headings.needs_human == "## Help Needed"

    def test_comment_headings_format_completion_comment(self):
        """Verify CommentHeadings produces correct format."""
        headings = CommentHeadings(
            implementation="## What I Did",
            problems="## Problems",
            pr_link="## PR Link",
        )

        comment = headings.format_completion_comment(
            implementation="Fixed the bug in foo.py",
            problems="None",
            pr_url="https://github.com/test/repo/pull/123"
        )

        assert "## What I Did" in comment
        assert "Fixed the bug" in comment
        assert "## Problems" in comment
        assert "## PR Link" in comment
        assert "https://github.com/test/repo/pull/123" in comment


class TestObserverWiring:
    """Test that observer correctly detects session states."""

    def test_observer_detects_completed_session(self, tmp_path: Path):
        """Verify observer detects when a session has completed."""
        from issue_orchestrator.observation import SessionObserver
        from issue_orchestrator.domain.models import Session, Issue, AgentConfig, SessionStatus
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        from datetime import datetime

        config = MagicMock()
        config.get_label_blocked.return_value = "blocked"
        config.get_label_needs_human.return_value = "needs-human"

        # Mock the session runner to report session not exists
        mock_runner = MagicMock()
        mock_runner.session_exists_by_name.return_value = False

        # Mock repository host for PR lookup
        mock_repo_host = MagicMock()
        mock_repo_host.get_open_prs_for_branch.return_value = [
            MagicMock(url="https://github.com/test/pull/1")
        ]

        observer = SessionObserver(
            config,
            FileSystemSessionOutput(),
            session_runner=mock_runner,
            repository_host=mock_repo_host,
        )

        issue = Issue(number=789, title="Test", labels=["agent:test"])
        issue_key = FakeIssueKey(name="789")
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        worktree = tmp_path / "worktree-789"
        worktree.mkdir(parents=True)
        terminal_id = "orchestrator"
        session = Session(
            key=session_key,
            issue=issue,
            agent_config=AgentConfig(
                prompt_path=Path("test.md"),
                timeout_minutes=60
            ),
            terminal_id=terminal_id,
            worktree_path=worktree,
            branch_name="789-test",
            started_at=datetime.now(),
            run_assets=make_session_run_assets(worktree, session_name=terminal_id),
        )

        # check_session is NOT async - it's a regular method
        status = observer.check_session(session)

        assert status == SessionStatus.COMPLETED

    def test_observer_detects_running_session(self, tmp_path: Path):
        """Verify observer detects when a session is still running."""
        from issue_orchestrator.observation import SessionObserver
        from issue_orchestrator.domain.models import Session, Issue, AgentConfig, SessionStatus
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        from datetime import datetime

        config = MagicMock()

        # Mock the session runner to report session exists
        mock_runner = MagicMock()
        mock_runner.session_exists_by_name.return_value = True

        observer = SessionObserver(config, FileSystemSessionOutput(), session_runner=mock_runner)

        issue = Issue(number=101, title="Test", labels=["agent:test"])
        issue_key = FakeIssueKey(name="101")
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        worktree = tmp_path / "worktree-101"
        worktree.mkdir(parents=True)
        terminal_id = "orchestrator"
        session = Session(
            key=session_key,
            issue=issue,
            agent_config=AgentConfig(
                prompt_path=Path("test.md"),
                timeout_minutes=60
            ),
            terminal_id=terminal_id,
            worktree_path=worktree,
            branch_name="101-test",
            started_at=datetime.now(),
            run_assets=make_session_run_assets(worktree, session_name=terminal_id),
        )

        status = observer.check_session(session)
        assert status == SessionStatus.RUNNING


class TestSmoke:
    """Smoke tests to verify basic functionality works end-to-end."""

    def test_can_import_all_modules(self):
        """Verify all modules can be imported without errors."""
        from issue_orchestrator.entrypoints import cli
        from issue_orchestrator.infra import config
        from issue_orchestrator.entrypoints import dashboard
        from issue_orchestrator.domain import models
        from issue_orchestrator.observation import observer
        from issue_orchestrator.infra import orchestrator
        from issue_orchestrator.control import scheduler
        from issue_orchestrator.adapters.worktree import _worktree as worktree
        from issue_orchestrator import execution

        # If we get here, all imports succeeded
        assert True

    def test_config_default_values(self):
        """Verify config has sensible defaults."""
        config = Config()
        assert config.max_concurrent_sessions == 3
        assert config.session_timeout_minutes == 45
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.comment_headings.implementation == "## Implementation"

    def test_orchestrator_state_initial(self):
        """Verify OrchestratorState starts with correct defaults."""
        state = OrchestratorState()
        assert state.active_sessions == []
        assert state.completed_today == []
        assert state.paused is False
        assert state.priority_queue == []
