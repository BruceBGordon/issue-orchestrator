"""Unit tests for data models."""

import time

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from issue_orchestrator.domain.pause_state import PauseActor, PauseReason, PauseState
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    IssueStatus,
    AgentConfig,
    DiscoveredRetrospectiveReview,
    OrchestratorState,
    PendingRetrospectiveReview,
    PendingValidationRetry,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets
from tests.conftest import operator_paused_state


def _make_session_key(issue_number: int = 1, task: TaskKind = TaskKind.CODE) -> SessionKey:
    """Helper to create a SessionKey for testing."""
    issue_key = FakeIssueKey(name=str(issue_number))
    return SessionKey(issue=issue_key, task=task)


class TestIssue:
    """Test the Issue data model."""

    def test_issue_creation(self):
        """Test basic issue creation."""
        issue = Issue(
            number=1,
            title="Test Issue",
            labels=["priority:high"],
            body="Test body",
        )
        assert issue.number == 1
        assert issue.title == "Test Issue"
        assert issue.body == "Test body"
        assert issue.labels == ["priority:high"]

    def test_agent_type_extraction(self):
        """Test extracting agent type from labels."""
        issue_web = Issue(
            number=1,
            title="Web task",
            labels=["agent:web", "priority:high"],
        )
        assert issue_web.agent_type == "agent:web"

        issue_mobile = Issue(
            number=2,
            title="Mobile task",
            labels=["priority:medium", "agent:mobile"],
        )
        assert issue_mobile.agent_type == "agent:mobile"

        issue_no_agent = Issue(
            number=3,
            title="No agent",
            labels=["priority:low"],
        )
        assert issue_no_agent.agent_type is None

    def test_priority_extraction(self):
        """Test priority extraction from labels."""
        high = Issue(number=1, title="High", labels=["priority:high"])
        assert high.priority == 1

        medium = Issue(number=2, title="Medium", labels=["priority:medium"])
        assert medium.priority == 2

        low = Issue(number=3, title="Low", labels=["priority:low"])
        assert low.priority == 3

        no_priority = Issue(number=4, title="No priority", labels=[])
        assert no_priority.priority == 4

    def test_priority_comparison(self):
        """Test that priority can be used for sorting."""
        issues = [
            Issue(number=1, title="Low", labels=["priority:low"]),
            Issue(number=2, title="High", labels=["priority:high"]),
            Issue(number=3, title="Medium", labels=["priority:medium"]),
        ]
        sorted_issues = sorted(issues, key=lambda i: i.priority)
        assert sorted_issues[0].number == 2  # High priority first
        assert sorted_issues[1].number == 3  # Medium second
        assert sorted_issues[2].number == 1  # Low last

    def test_is_blocked(self):
        """Test is_blocked property."""
        blocked = Issue(number=1, title="Blocked", labels=["blocked"])
        assert blocked.is_blocked

        not_blocked = Issue(number=2, title="Not blocked", labels=[])
        assert not not_blocked.is_blocked

    def test_is_in_progress(self):
        """Test is_in_progress property."""
        in_progress = Issue(number=1, title="In progress", labels=["in-progress"])
        assert in_progress.is_in_progress

        not_in_progress = Issue(number=2, title="Not in progress", labels=[])
        assert not not_in_progress.is_in_progress

    def test_needs_human(self):
        """Test needs_human property."""
        needs_human = Issue(number=1, title="Needs human", labels=["needs-human"])
        assert needs_human.needs_human

        no_needs = Issue(number=2, title="No needs", labels=[])
        assert not no_needs.needs_human

    def test_issue_with_all_properties(self):
        """Test issue with multiple labels."""
        issue = Issue(
            number=1,
            title="Complex issue",
            labels=["agent:web", "priority:high", "needs-human"],
            milestone="M6",
            body="This needs work",
        )
        assert issue.agent_type == "agent:web"
        assert issue.priority == 1
        assert issue.needs_human
        assert issue.milestone == "M6"


class TestAgentConfig:
    """Test the AgentConfig data model."""

    def test_agent_config_creation(self, tmp_path):
        """Test basic agent config creation."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            model="sonnet",
            timeout_minutes=45,
        )

        assert config.prompt_path == prompt_file
        assert config.model == "sonnet"
        assert config.timeout_minutes == 45

    def test_agent_config_defaults(self, tmp_path):
        """Test agent config with default values."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
        )

        assert config.model == "sonnet"
        assert config.timeout_minutes == 45

    def test_effective_permission_mode_provider_args_wins(self, tmp_path):
        """provider_args.permission_mode is the single config spelling."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            provider="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        )

        assert config.effective_permission_mode == "bypassPermissions"

    def test_effective_permission_mode_defaults_when_unset(self, tmp_path):
        """Without provider_args.permission_mode, the mode is 'default'."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(prompt_path=prompt_file)

        assert config.effective_permission_mode == "default"

    def test_provider_args_permission_mode_reaches_launch_command(self, tmp_path):
        """A claude agent configured via provider_args launches with that mode."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            provider="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        )

        command = config.get_command(
            issue_number=1, issue_title="Title", worktree=tmp_path
        )

        assert "--permission-mode bypassPermissions" in command

    def test_claude_opus_effort_reaches_launch_command(self, tmp_path):
        """Claude model and effort are configured through model/provider_args."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            provider="claude-code",
            model="opus",
            provider_args={"effort": "xhigh"},
        )

        command = config.get_command(
            issue_number=1, issue_title="Title", worktree=tmp_path
        )

        import shlex

        tokens = shlex.split(command)
        assert tokens[:5] == ["claude", "--model", "opus", "--effort", "xhigh"]

    def test_review_task_kind_defaults_to_review_initial_prompt(self, tmp_path):
        """Review launches without an explicit initial_prompt get the review
        default, never the coding-flavored field default."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(prompt_path=prompt_file)

        rendered = config.render_initial_prompt(
            issue_number=7,
            issue_title="Title",
            worktree=tmp_path,
            pr_number=42,
            task_kind=TaskKind.REVIEW.value,
        )

        assert "reviewer-done" in rendered
        assert "PR #42" in rendered
        assert "coding-done" not in rendered

    def test_retrospective_review_task_kind_defaults_to_review_initial_prompt(
        self, tmp_path
    ):
        """Retrospective review launches also get the review default."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(prompt_path=prompt_file)

        rendered = config.render_initial_prompt(
            issue_number=7,
            issue_title="Title",
            worktree=tmp_path,
            pr_number=42,
            task_kind=TaskKind.RETROSPECTIVE_REVIEW.value,
        )

        assert "reviewer-done" in rendered
        assert "coding-done" not in rendered

    def test_code_task_kind_keeps_coding_initial_prompt(self, tmp_path):
        """Code launches keep the coding default initial prompt."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(prompt_path=prompt_file)

        rendered = config.render_initial_prompt(
            issue_number=7,
            issue_title="Title",
            worktree=tmp_path,
            task_kind=TaskKind.CODE.value,
        )

        assert "coding-done" in rendered
        assert "reviewer-done" not in rendered

    def test_explicit_initial_prompt_wins_for_review_task_kind(self, tmp_path):
        """An explicitly configured initial_prompt is used as-is for any kind."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            initial_prompt="Custom review flow for #{issue_number}",
        )

        rendered = config.render_initial_prompt(
            issue_number=7,
            issue_title="Title",
            worktree=tmp_path,
            pr_number=42,
            task_kind=TaskKind.REVIEW.value,
        )

        assert rendered == "Custom review flow for #7"

    def test_get_command_review_kind_uses_review_initial_prompt(self, tmp_path):
        """The review default flows through get_command for review launches."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(prompt_path=prompt_file, provider="claude-code")

        command = config.get_command(
            issue_number=7,
            issue_title="Title",
            worktree=tmp_path,
            pr_number=42,
            task_kind=TaskKind.REVIEW.value,
        )

        assert "use reviewer-done to report your verdict" in command

    def test_resolve_launch_provider_prefers_explicit_provider(self, tmp_path):
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(
            prompt_path=prompt_file, provider="codex", ai_system="claude-code",
        )
        assert config.resolve_launch_provider() == "codex"

    def test_resolve_launch_provider_falls_back_to_ai_system(self, tmp_path):
        """An ai_system-only agent must launch its REAL agent, not the legacy
        claude template — classification and launch must agree (the
        real-codex exchange smoke test caught an ai_system="codex" reviewer
        silently booting print-mode claude and hanging the round)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(prompt_path=prompt_file, ai_system="codex")
        assert config.resolve_launch_provider() == "codex"

    def test_resolve_launch_provider_honors_custom_command_override(self, tmp_path):
        """A custom command template is an intentional override (exchange
        stub agents set one alongside a real ai_system) — it must keep
        template-based launching."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(
            prompt_path=prompt_file, ai_system="codex", command="python -u stub.py",
        )
        assert config.resolve_launch_provider() is None

    def test_resolve_launch_provider_none_when_nothing_configured(self, tmp_path):
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(prompt_path=prompt_file)
        assert config.resolve_launch_provider() is None

    def test_provider_command_omits_claude_default_model_for_codex(self, tmp_path):
        """The untouched model default ("sonnet") is claude vocabulary; it
        must not be forwarded to codex (``--model sonnet`` gets a 400 from
        the codex backend and the TUI idles for the whole round timeout —
        caught live by the real-codex exchange smoke test)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(prompt_path=prompt_file, provider="codex")
        command = config.get_command(
            issue_number=1, issue_title="t", worktree=tmp_path,
        )
        assert "--model" not in command

    def test_provider_command_forwards_explicit_model_for_codex(self, tmp_path):
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(
            prompt_path=prompt_file, provider="codex", model="gpt-5-codex",
        )
        command = config.get_command(
            issue_number=1, issue_title="t", worktree=tmp_path,
        )
        assert "--model gpt-5-codex" in command

    def test_provider_command_keeps_default_model_for_claude(self, tmp_path):
        """claude-code owns the "sonnet" default — forwarding it is correct
        and preserves existing behavior."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("p")
        config = AgentConfig(prompt_path=prompt_file, provider="claude-code")
        command = config.get_command(
            issue_number=1, issue_title="t", worktree=tmp_path,
        )
        assert "--model sonnet" in command

    def test_get_command_template_includes_system_prompt_variable(self, tmp_path):
        """Test custom command path includes {system_prompt} with completion command docs."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Should include completion command docs
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        # Should include instruction to read prompt file
        assert "prompt.md" in cmd

    def test_get_command_provider_always_injects_agent_done(self, tmp_path):
        """Test provider path always injects completion command docs (strict enforcement)."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Should include completion command docs in system prompt
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        assert "prompt.md" in cmd
        # Interactive mode: no -p, no stream-json
        assert "-p" not in cmd.split()
        assert "--output-format" not in cmd
        assert "--include-partial-messages" not in cmd

    def test_get_command_provider_appends_user_system_prompt(self, tmp_path):
        """Test user-provided system_prompt is appended, not replaced."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
            provider_args={"system_prompt": "Custom user instructions here"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Should include BOTH completion command docs AND user content
        assert "CRITICAL" in cmd  # Completion command enforcement
        assert "coding-done" in cmd
        assert "Custom user instructions here" in cmd  # User content appended
        # Completion command docs should come BEFORE user content in the command
        critical_pos = cmd.find("CRITICAL")
        user_pos = cmd.find("Custom user instructions")
        assert critical_pos < user_pos, "Completion command docs must come before user system_prompt"

    def test_get_command_provider_user_system_prompt_cannot_replace(self, tmp_path):
        """Test user cannot replace completion command injection even with matching key."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        # User tries to provide their own system_prompt that omits completion commands
        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
            provider_args={"system_prompt": "My own instructions without agent-done"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Completion commands MUST still be present (strict enforcement)
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        # User content is also present (extensibility)
        assert "My own instructions without agent-done" in cmd

    def test_get_command_codex_provider_injects_agent_done(self, tmp_path):
        """Test Codex provider also gets completion command injection (universal enforcement)."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="codex",
            model="gpt-5-codex",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Completion commands MUST be present even for non-Claude providers
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        assert "prompt.md" in cmd
        # Should use codex executable
        assert "codex" in cmd

    def test_get_command_non_claude_provider_prepends_to_prompt(self, tmp_path):
        """Test non-Claude providers get completion command docs prepended to prompt."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="codex",
            model="gpt-5-codex",
            initial_prompt="Do the work on issue #{issue_number}",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Both completion command docs and the initial prompt should be in the command
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        assert "Do the work on issue #123" in cmd
        # Completion command docs should come before the user's initial prompt
        critical_pos = cmd.find("CRITICAL")
        user_prompt_pos = cmd.find("Do the work on issue #123")
        assert critical_pos < user_prompt_pos, "Completion command docs must precede user prompt"


    def test_get_command_extra_provider_args_verbose(self, tmp_path):
        """extra_provider_args override merges onto provider_args."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
        )

        cmd = config.get_command(
            123, "Test Issue", tmp_path,
            extra_provider_args={"verbose": "true"},
        )
        # Check --verbose appears as an actual CLI flag (shlex-joined token)
        import shlex
        tokens = shlex.split(cmd)
        assert "--verbose" in tokens

    def test_get_command_extra_provider_args_none_is_safe(self, tmp_path):
        """extra_provider_args=None should work fine (default)."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)
        import shlex
        tokens = shlex.split(cmd)
        assert "--verbose" not in tokens

    def test_get_command_provider_args_verbose_from_agent_config(self, tmp_path):
        """provider_args.verbose in agent config passes --verbose."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
            provider_args={"verbose": "true"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)
        import shlex
        tokens = shlex.split(cmd)
        assert "--verbose" in tokens

    def test_get_command_codex_provider_args_reasoning_effort(self, tmp_path):
        """Codex provider args expose reasoning effort through normal config."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="codex",
            model="gpt-5.4",
            provider_args={"reasoning_effort": "xhigh"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        import shlex
        tokens = shlex.split(cmd)
        assert tokens[0] == "env"
        assert tokens[1].startswith("CODEX_HOME=")
        assert tokens[2] == "codex"
        assert "exec" not in tokens[:3]
        assert "--ask-for-approval" in tokens
        assert "never" in tokens
        assert "gpt-5.4" in tokens
        assert 'model_reasoning_effort="xhigh"' in tokens


class TestSession:
    """Test the Session data model."""

    def test_session_creation(self, sample_agent_config, sample_issues):
        """Test basic session creation."""
        issue = sample_issues[0]
        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="test-session",
            ),
        )

        assert session.issue == issue
        assert session.agent_config == sample_agent_config
        assert session.terminal_id == "test-session"
        assert session.worktree_path == Path("/tmp/worktree")
        assert session.branch_name == "feature/test"
        assert session.status == SessionStatus.RUNNING

    def test_session_runtime_minutes(self, sample_agent_config, sample_issues):
        """Test runtime calculation."""
        issue = sample_issues[0]
        now = datetime.now()
        past = now - timedelta(minutes=30)

        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="test-session",
            ),
            started_at=past,
            watchdog_started_at_monotonic=time.monotonic() - (30 * 60),
        )

        # Allow small variance due to test execution time
        runtime = session.runtime_minutes
        assert 29 <= runtime <= 31

    def test_session_is_timed_out_false(self, sample_agent_config, sample_issues):
        """Test is_timed_out when session is still running."""
        issue = sample_issues[0]
        now = datetime.now()
        recent = now - timedelta(minutes=10)

        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="test-session",
            ),
            started_at=recent,
            watchdog_started_at_monotonic=time.monotonic() - (10 * 60),
        )

        assert not session.is_timed_out

    def test_session_is_timed_out_true(self, sample_agent_config, sample_issues):
        """Test is_timed_out when session exceeded timeout."""
        issue = sample_issues[0]
        config = AgentConfig(
            prompt_path=sample_agent_config.prompt_path,
            timeout_minutes=30,
        )

        now = datetime.now()
        old = now - timedelta(minutes=60)  # 60 minutes ago

        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="test-session",
            ),
            started_at=old,
            watchdog_started_at_monotonic=time.monotonic() - (60 * 60),
        )

        assert session.is_timed_out

    def test_session_status_change(self, sample_agent_config, sample_issues):
        """Test session status transitions."""
        issue = sample_issues[0]
        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            run_assets=make_session_run_assets(
                Path("/tmp/worktree"),
                session_name="test-session",
            ),
        )

        assert session.status == SessionStatus.RUNNING

        session.status = SessionStatus.COMPLETED
        assert session.status == SessionStatus.COMPLETED

        session.status = SessionStatus.BLOCKED
        assert session.status == SessionStatus.BLOCKED


class TestSessionStatus:
    """Test the SessionStatus enum."""

    def test_session_status_values(self):
        """Test all session status values."""
        assert SessionStatus.PENDING.value == "pending"
        assert SessionStatus.RUNNING.value == "running"
        assert SessionStatus.COMPLETED.value == "completed"
        assert SessionStatus.BLOCKED.value == "blocked"
        assert SessionStatus.NEEDS_HUMAN.value == "needs_human"
        assert SessionStatus.FAILED.value == "failed"
        assert SessionStatus.TIMED_OUT.value == "timed_out"


class TestIssueStatus:
    """Test the IssueStatus enum."""

    def test_issue_status_values(self):
        """Test all issue status values."""
        assert IssueStatus.AVAILABLE.value == "available"
        assert IssueStatus.IN_PROGRESS.value == "in_progress"
        assert IssueStatus.BLOCKED.value == "blocked"
        assert IssueStatus.NEEDS_HUMAN.value == "needs_human"
        assert IssueStatus.COMPLETED.value == "completed"


class TestOrchestratorState:
    """Test the OrchestratorState data model."""

    def test_orchestrator_state_creation(self):
        """Test basic orchestrator state creation."""
        state = OrchestratorState()

        assert state.active_sessions == []
        assert state.completed_today == []
        assert state.paused is False
        assert state.priority_queue == []
        assert state.pending_retrospective_reviews == []
        assert state.discovered_retrospective_reviews == []
        assert state.retrospective_review_in_flight_issue_numbers() == set()
        assert state.pending_or_active_retrospective_review_issue_numbers() == set()
        assert state.has_in_flight_retrospective_review(365) is False
        assert state.has_pending_or_active_retrospective_review(365) is False

    def test_retrospective_review_in_flight_numbers_cover_all_sources(
        self,
        sample_agent_config,
    ):
        """Retrospective review duplicate checks include queued, discovered, and active work."""
        active_issue = Issue(number=367, title="Active", labels=["agent:web"])
        active_session = Session(
            key=_make_session_key(367, TaskKind.RETROSPECTIVE_REVIEW),
            issue=active_issue,
            agent_config=sample_agent_config,
            terminal_id="retrospective-review-367",
            worktree_path=Path("/tmp/work367"),
            branch_name="issue-367",
            run_assets=make_session_run_assets(
                Path("/tmp/work367"),
                session_name="retrospective-review-367",
            ),
        )
        restored_legacy_issue = Issue(number=368, title="Restored", labels=["agent:web"])
        restored_legacy_session = Session(
            key=_make_session_key(368, TaskKind.CODE),
            issue=restored_legacy_issue,
            agent_config=sample_agent_config,
            terminal_id="retrospective-review-368",
            worktree_path=Path("/tmp/work368"),
            branch_name="issue-368",
            run_assets=make_session_run_assets(
                Path("/tmp/work368"),
                session_name="retrospective-review-368",
            ),
        )
        state = OrchestratorState(
            active_sessions=[active_session, restored_legacy_session],
            pending_retrospective_reviews=[
                PendingRetrospectiveReview(
                    issue_key=FakeIssueKey("365"),
                    issue_number=365,
                    issue_title="Pending",
                    agent_label="agent:web",
                    trigger_label="lack-of-review-redo",
                )
            ],
            discovered_retrospective_reviews=[
                DiscoveredRetrospectiveReview(
                    issue_number=366,
                    issue_title="Discovered",
                    agent_label="agent:web",
                    trigger_label="lack-of-review-redo",
                    issue_key="366",
                )
            ],
        )

        assert state.retrospective_review_in_flight_issue_numbers() == {
            365,
            366,
            367,
            368,
        }
        assert state.pending_or_active_retrospective_review_issue_numbers() == {
            365,
            367,
            368,
        }
        assert state.has_in_flight_retrospective_review(366) is True
        assert state.has_pending_or_active_retrospective_review(366) is False
        assert state.has_in_flight_retrospective_review(369) is False

    def test_orchestrator_state_with_data(self, sample_agent_config, sample_issues):
        """Test orchestrator state with populated data."""
        session1 = Session(
            key=_make_session_key(sample_issues[0].number),
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            terminal_id="session-1",
            worktree_path=Path("/tmp/work1"),
            branch_name="feature/1",
            run_assets=make_session_run_assets(
                Path("/tmp/work1"),
                session_name="session-1",
            ),
        )

        state = OrchestratorState(
            active_sessions=[session1],
            completed_today=[1, 2, 3],
            pause_state=operator_paused_state(),
            priority_queue=[4, 5],
        )

        assert len(state.active_sessions) == 1
        assert state.active_sessions[0] == session1
        assert state.completed_today == [1, 2, 3]
        assert state.paused is True
        assert state.priority_queue == [4, 5]

    def test_orchestrator_state_pause_toggle(self):
        """``paused`` projects ``pause_state`` and cannot be written directly."""
        state = OrchestratorState()
        assert state.paused is False

        state.pause_state = PauseState.paused_now(
            reason=PauseReason.OPERATOR, actor=PauseActor.CONTROL_API
        )
        assert state.paused is True

        state.pause_state = PauseState.running()
        assert state.paused is False

    def test_paused_cannot_be_assigned_directly(self):
        """The write path that used to skip the event, log, and journal.

        Two entrypoints set ``state.paused = True`` straight onto the dataclass,
        which recorded the pause nowhere. Writes must fail loudly so they are
        routed through PauseController instead.
        """
        state = OrchestratorState()
        with pytest.raises(AttributeError, match="no setter"):
            state.paused = True


class TestPendingValidationRetry:
    """Guard tests for the validation-retry queue/recovery owner boundary."""

    @staticmethod
    def _build(source_task: TaskKind) -> PendingValidationRetry:
        return PendingValidationRetry(
            issue_number=42,
            issue_title="Issue 42",
            agent_label="agent:backend",
            worktree_path="/tmp/issue-42",
            branch_name="42-fix",
            original_prompt=None,
            validation_error="dirty tree",
            validation_error_file=None,
            retry_count=1,
            source_task=source_task,
        )

    def test_accepts_coding_source_tasks(self):
        """Coding-style sources produce a queueable retry."""
        for task in (TaskKind.CODE, TaskKind.REWORK):
            retry = self._build(task)
            assert retry.source_task is task

    @pytest.mark.parametrize(
        "review_task",
        [TaskKind.REVIEW, TaskKind.RETROSPECTIVE_REVIEW],
    )
    def test_rejects_review_only_source_task(self, review_task):
        """A review-only session can never append a coding validation retry (#6426)."""
        with pytest.raises(ValueError, match="review-only"):
            self._build(review_task)
