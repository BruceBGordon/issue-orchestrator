"""Unit tests for the completion command shared core and split CLI entry points.

Tests cover:
- agent_done shared utilities (statuses, fields, serialization, record building)
- coding-done main() for coding/rework agent completion (completed, blocked, needs_human)
- reviewer-done main() for review agent completion (approved, changes_requested)

The orchestrator handles all side effects (push, PR, comments, labels).
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.entrypoints.cli_tools.agent_done import (
    AgentStatus,
    REQUIRED_FIELDS,
    STATUS_TO_OUTCOME,
    STATUS_TO_ACTIONS,
    load_follow_up_issues,
    WorktreeMismatchError,
    die,
    find_worktree_root,
    get_issue_number,
    get_session_id,
    validate_fields,
    format_comment_body,
    build_completion_record,
    write_completion_record,
    write_marker_file,
    record_validation_artifacts,
)
from issue_orchestrator.control.validation import AgentGateResult
from issue_orchestrator.entrypoints.cli_tools.coding_done import (
    main as coding_done_main,
    check_dirty_files,
)
from issue_orchestrator.entrypoints.cli_tools.reviewer_done import main as reviewer_done_main
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    RequestedAction,
    CompletionRecord,
    ProposedFollowUpIssue,
    COMPLETION_RECORD_PATH,
)
from issue_orchestrator.ports.session_output import ValidationRecord


def _orchestrator_env(run_dir: Path, *, session_id: str = "test-123") -> dict[str, str]:
    return {
        f"{ENV_PREFIX}SESSION_ID": session_id,
        "ORCHESTRATOR_SESSION_ID": session_id,
        f"{ENV_PREFIX}RUN_DIR": str(run_dir),
    }


class TestAgentStatus:
    """Test the AgentStatus constants."""

    def test_status_values(self):
        """Test all status values are correct."""
        assert AgentStatus.COMPLETED == "completed"
        assert AgentStatus.BLOCKED == "blocked"
        assert AgentStatus.NEEDS_HUMAN == "needs_human"
        assert AgentStatus.APPROVED == "approved"
        assert AgentStatus.CHANGES_REQUESTED == "changes_requested"


class TestRequiredFields:
    """Test REQUIRED_FIELDS constant."""

    def test_completed_required_fields(self):
        """Test required fields for completed status."""
        assert REQUIRED_FIELDS[AgentStatus.COMPLETED] == ["implementation", "problems"]

    def test_blocked_required_fields(self):
        """Test required fields for blocked status."""
        assert REQUIRED_FIELDS[AgentStatus.BLOCKED] == ["reason", "attempted"]

    def test_needs_human_required_fields(self):
        """Test required fields for needs_human status."""
        assert REQUIRED_FIELDS[AgentStatus.NEEDS_HUMAN] == ["question"]

    def test_approved_required_fields(self):
        """Test required fields for approved status include risk."""
        assert REQUIRED_FIELDS[AgentStatus.APPROVED] == ["summary", "risk"]

    def test_changes_requested_required_fields(self):
        """Test required fields for changes_requested status include risk."""
        assert REQUIRED_FIELDS[AgentStatus.CHANGES_REQUESTED] == ["issues", "risk"]


class TestStatusToOutcome:
    """Test STATUS_TO_OUTCOME mapping."""

    def test_completed_maps_to_completed(self):
        """Test completed status maps to COMPLETED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.COMPLETED] == CompletionOutcome.COMPLETED

    def test_blocked_maps_to_blocked(self):
        """Test blocked status maps to BLOCKED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.BLOCKED] == CompletionOutcome.BLOCKED

    def test_needs_human_maps_to_needs_human(self):
        """Test needs_human status maps to NEEDS_HUMAN outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.NEEDS_HUMAN] == CompletionOutcome.NEEDS_HUMAN

    def test_approved_maps_to_review_approved(self):
        """Test approved status maps to REVIEW_APPROVED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.APPROVED] == CompletionOutcome.REVIEW_APPROVED

    def test_changes_requested_maps_to_review_changes_requested(self):
        """Test changes_requested status maps to REVIEW_CHANGES_REQUESTED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.CHANGES_REQUESTED] == CompletionOutcome.REVIEW_CHANGES_REQUESTED


class TestStatusToActions:
    """Test STATUS_TO_ACTIONS mapping."""

    def test_completed_actions(self):
        """Test completed status requests push, PR, and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.COMPLETED]
        assert RequestedAction.PUSH_BRANCH in actions
        assert RequestedAction.CREATE_PR in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_blocked_actions(self):
        """Test blocked status requests push, label, and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.BLOCKED]
        assert RequestedAction.PUSH_BRANCH in actions
        assert RequestedAction.ADD_BLOCKED_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_needs_human_actions(self):
        """Test needs_human status requests push, label, and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.NEEDS_HUMAN]
        assert RequestedAction.PUSH_BRANCH in actions
        assert RequestedAction.ADD_NEEDS_HUMAN_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_approved_actions(self):
        """Test approved status requests label changes and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.APPROVED]
        assert RequestedAction.ADD_CODE_REVIEWED_LABEL in actions
        assert RequestedAction.REMOVE_NEEDS_REWORK_LABEL in actions
        assert RequestedAction.REMOVE_CODE_REVIEW_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_changes_requested_actions(self):
        """Test changes_requested status requests label changes and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.CHANGES_REQUESTED]
        assert RequestedAction.ADD_NEEDS_REWORK_LABEL in actions
        assert RequestedAction.REMOVE_CODE_REVIEW_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions


class TestDie:
    """Test the die function."""

    def test_die_exits_with_1(self):
        """Test die exits with status code 1."""
        with pytest.raises(SystemExit) as exc_info:
            die("Test error message")
        assert exc_info.value.code == 1

    def test_die_prints_to_stderr(self, capsys):
        """Test die prints error message to stderr."""
        with pytest.raises(SystemExit):
            die("Test error message")
        captured = capsys.readouterr()
        assert "ERROR: Test error message" in captured.err
        assert "Use --help for usage information" in captured.err


class TestGetSessionId:
    """Test the get_session_id function."""

    def test_get_session_id_from_env(self):
        """Test getting session ID from environment variable."""
        with patch.dict(os.environ, {"ORCHESTRATOR_SESSION_ID": "test-session-123"}):
            session_id = get_session_id()
            assert session_id == "test-session-123"

    def test_get_session_id_standalone_fallback(self):
        """Test standalone session ID when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)
            session_id = get_session_id()
            assert session_id.startswith("standalone-")
            # Should contain date-time pattern
            assert "-" in session_id

    def test_prefers_prefixed_session_id_env(self):
        """ISSUE_ORCHESTRATOR_SESSION_ID should take precedence."""
        prefixed = f"{ENV_PREFIX}SESSION_ID"
        with patch.dict(
            os.environ,
            {prefixed: "prefixed-session", "ORCHESTRATOR_SESSION_ID": "legacy-session"},
            clear=True,
        ):
            assert get_session_id() == "prefixed-session"


class TestGetIssueNumber:
    """Test issue number resolution from environment."""

    def test_prefers_prefixed_issue_number_env(self):
        prefixed = f"{ENV_PREFIX}ISSUE_NUMBER"
        with patch.dict(
            os.environ,
            {prefixed: "4057", "ORCHESTRATOR_ISSUE_NUMBER": "1"},
            clear=True,
        ):
            assert get_issue_number() == 4057


class TestValidateFields:
    """Test the validate_fields function."""

    def _make_args(self, **kwargs):
        """Helper to create argparse.Namespace with given values."""
        defaults = {
            "implementation": None,
            "problems": None,
            "follow_up_file": None,
            "reason": None,
            "attempted": None,
            "blocked_by": None,
            "when_unblocked": None,
            "question": None,
            "context": None,
            "options": None,
            "default": None,
            "summary": None,
            "issues": None,
            "risk": None,
            "checks": None,
            "checks_needed": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_validate_completed_with_all_fields(self):
        """Test validation passes for completed with all required fields."""
        args = self._make_args(
            implementation="Added feature",
            problems="None",
        )
        # Should not raise
        validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_completed_missing_implementation(self):
        """Test validation fails when implementation is missing."""
        args = self._make_args(problems="None")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_completed_missing_problems(self):
        """Test validation fails when problems is missing."""
        args = self._make_args(implementation="Added feature")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_completed_empty_implementation(self):
        """Test validation fails when implementation is empty string."""
        args = self._make_args(
            implementation="   ",
            problems="None",
        )
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_blocked_with_all_fields(self):
        """Test validation passes for blocked with all required fields."""
        args = self._make_args(
            reason="Waiting for API",
            attempted="Tried workaround",
        )
        validate_fields(AgentStatus.BLOCKED, args)

    def test_validate_blocked_missing_reason(self):
        """Test validation fails when reason is missing."""
        args = self._make_args(attempted="Tried workaround")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.BLOCKED, args)

    def test_validate_blocked_missing_attempted(self):
        """Test validation fails when attempted is missing."""
        args = self._make_args(reason="Waiting for API")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.BLOCKED, args)

    def test_validate_needs_human_with_question(self):
        """Test validation passes for needs_human with question."""
        args = self._make_args(question="Which approach?")
        validate_fields(AgentStatus.NEEDS_HUMAN, args)

    def test_validate_needs_human_missing_question(self):
        """Test validation fails when question is missing."""
        args = self._make_args()
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.NEEDS_HUMAN, args)

    def test_validate_approved_with_all_fields(self):
        """Test validation passes for approved with summary and risk."""
        args = self._make_args(
            summary="Code looks good",
            risk="low",
        )
        validate_fields(AgentStatus.APPROVED, args)

    def test_validate_approved_missing_summary(self):
        """Test validation fails when summary is missing."""
        args = self._make_args(risk="low")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.APPROVED, args)

    def test_validate_approved_missing_risk(self):
        """Test validation fails when risk is missing."""
        args = self._make_args(summary="LGTM")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.APPROVED, args)

    def test_validate_changes_requested_with_all_fields(self):
        """Test validation passes for changes_requested with issues and risk."""
        args = self._make_args(
            issues="Need more tests",
            risk="medium",
        )
        validate_fields(AgentStatus.CHANGES_REQUESTED, args)

    def test_validate_changes_requested_missing_issues(self):
        """Test validation fails when issues is missing."""
        args = self._make_args(risk="high")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.CHANGES_REQUESTED, args)

    def test_validate_changes_requested_missing_risk(self):
        """Test validation fails when risk is missing."""
        args = self._make_args(issues="Security issue")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.CHANGES_REQUESTED, args)


class TestFormatCommentBody:
    """Test the format_comment_body function."""

    def _make_args(self, **kwargs):
        """Helper to create argparse.Namespace with given values."""
        defaults = {
            "implementation": None,
            "problems": None,
            "reason": None,
            "attempted": None,
            "blocked_by": None,
            "when_unblocked": None,
            "question": None,
            "context": None,
            "options": None,
            "default": None,
            "summary": None,
            "issues": None,
            "risk": None,
            "checks": None,
            "checks_needed": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_format_completed_comment(self):
        """Test formatting completed comment."""
        args = self._make_args(
            implementation="Added user authentication",
            problems="None",
        )
        comment = format_comment_body(AgentStatus.COMPLETED, args)
        assert "## Implementation" in comment
        assert "Added user authentication" in comment
        assert "## Problems Encountered" in comment
        assert "None" in comment

    def test_format_blocked_comment_basic(self):
        """Test formatting basic blocked comment."""
        args = self._make_args(
            reason="Need API credentials",
            attempted="Checked environment variables",
            blocked_by=None,
            when_unblocked=None,
        )
        comment = format_comment_body(AgentStatus.BLOCKED, args)
        assert "## Blocked" in comment
        assert "**Reason:** Need API credentials" in comment
        assert "**Attempted:** Checked environment variables" in comment

    def test_format_blocked_comment_with_blocked_by(self):
        """Test formatting blocked comment with blocked_by issues."""
        args = self._make_args(
            reason="Depends on other work",
            attempted="Tried to work around it",
            blocked_by=[123, 456],
        )
        comment = format_comment_body(AgentStatus.BLOCKED, args)
        assert "**Blocked by:** #123, #456" in comment

    def test_format_blocked_comment_with_when_unblocked(self):
        """Test formatting blocked comment with when_unblocked hint."""
        args = self._make_args(
            reason="Waiting for API docs",
            attempted="Searched existing docs",
            when_unblocked="Implement auth flow using new endpoints",
        )
        comment = format_comment_body(AgentStatus.BLOCKED, args)
        assert "**When unblocked:** Implement auth flow using new endpoints" in comment

    def test_format_needs_human_comment_basic(self):
        """Test formatting basic needs_human comment."""
        args = self._make_args(
            question="Should we use approach A or B?",
        )
        comment = format_comment_body(AgentStatus.NEEDS_HUMAN, args)
        assert "## Needs Human Input" in comment
        assert "**Question:** Should we use approach A or B?" in comment

    def test_format_needs_human_comment_with_options(self):
        """Test formatting needs_human comment with options."""
        args = self._make_args(
            question="Which framework?",
            options=["React", "Vue", "Angular"],
        )
        comment = format_comment_body(AgentStatus.NEEDS_HUMAN, args)
        assert "**Options:**" in comment
        assert "1. React" in comment
        assert "2. Vue" in comment
        assert "3. Angular" in comment

    def test_format_needs_human_comment_with_default(self):
        """Test formatting needs_human comment with default action."""
        args = self._make_args(
            question="Proceed with deployment?",
            default="Will deploy in 24 hours",
        )
        comment = format_comment_body(AgentStatus.NEEDS_HUMAN, args)
        assert "**Default if no response:** Will deploy in 24 hours" in comment

    def test_format_approved_comment(self):
        """Test formatting approved comment with structured verdict."""
        args = self._make_args(
            summary="Code is clean and well-tested",
            risk="low",
            checks=["tests_added", "follows_patterns"],
        )
        comment = format_comment_body(AgentStatus.APPROVED, args)
        assert "## Code Review Approved" in comment
        assert "Code is clean and well-tested" in comment
        assert "**Verdict:** `approve`" in comment
        assert "`low`" in comment
        assert "`tests_added`" in comment

    def test_format_changes_requested_comment(self):
        """Test formatting changes_requested comment with structured verdict."""
        args = self._make_args(
            issues="Missing error handling in auth module",
            risk="medium",
            checks_needed=["error_handling", "tests_added"],
        )
        comment = format_comment_body(AgentStatus.CHANGES_REQUESTED, args)
        assert "## Changes Requested" in comment
        assert "Missing error handling in auth module" in comment
        assert "**Verdict:** `request_changes`" in comment
        assert "`medium`" in comment
        assert "`error_handling`" in comment


class TestBuildCompletionRecord:
    """Test the build_completion_record function."""

    def _make_args(self, **kwargs):
        """Helper to create argparse.Namespace with given values."""
        defaults = {
            "implementation": None,
            "problems": None,
            "reason": None,
            "attempted": None,
            "blocked_by": None,
            "when_unblocked": None,
            "question": None,
            "context": None,
            "options": None,
            "default": None,
            "summary": None,
            "issues": None,
            "risk": None,
            "checks": None,
            "checks_needed": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_build_completed_record(self):
        """Test building completion record for completed status."""
        args = self._make_args(
            implementation="Added user auth",
            problems="None",
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.COMPLETED, args)

        assert record.session_id == "test-session"
        assert record.outcome == CompletionOutcome.COMPLETED
        assert record.implementation == "Added user auth"
        assert record.problems == "None"
        assert RequestedAction.PUSH_BRANCH in record.requested_actions
        assert RequestedAction.CREATE_PR in record.requested_actions

    def test_build_completed_record_with_follow_up_file(self, tmp_path: Path):
        follow_up_path = tmp_path / "followups.jsonl"
        follow_up_path.write_text(
            json.dumps({
                "title": "Fix env-sensitive logging test isolation",
                "reason": "The test was unrelated to the assigned issue and kept pulling the agent off-scope.",
                "suggested_labels": ["bug", "tests"],
            }) + "\n",
            encoding="utf-8",
        )
        args = self._make_args(
            implementation="Added user auth",
            problems="None",
            follow_up_file=str(follow_up_path),
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.COMPLETED, args)

        assert record.follow_up_issues == [
            ProposedFollowUpIssue(
                title="Fix env-sensitive logging test isolation",
                reason="The test was unrelated to the assigned issue and kept pulling the agent off-scope.",
                suggested_labels=["bug", "tests"],
                blocking=False,
            )
        ]

    def test_build_blocked_record(self):
        """Test building completion record for blocked status."""
        args = self._make_args(
            reason="Need API key",
            attempted="Checked env",
            blocked_by=[123],
            when_unblocked="Implement auth",
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.BLOCKED, args)

        assert record.outcome == CompletionOutcome.BLOCKED
        assert record.blocked_reason == "Need API key"
        assert record.attempted == "Checked env"
        assert record.blocked_by == [123]
        assert record.when_unblocked == "Implement auth"
        assert RequestedAction.ADD_BLOCKED_LABEL in record.requested_actions

    def test_build_needs_human_record(self):
        """Test building completion record for needs_human status."""
        args = self._make_args(
            question="Which approach?",
            context="Two options available",
            options=["A", "B"],
            default="A",
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.NEEDS_HUMAN, args)

        assert record.outcome == CompletionOutcome.NEEDS_HUMAN
        assert record.question == "Which approach?"
        assert record.context == "Two options available"
        assert record.options == ["A", "B"]
        assert record.default_action == "A"
        assert RequestedAction.ADD_NEEDS_HUMAN_LABEL in record.requested_actions

    def test_build_approved_record(self):
        """Test building completion record for approved status."""
        args = self._make_args(
            summary="LGTM",
            risk="low",
            checks=["tests_added"],
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.APPROVED, args)

        assert record.outcome == CompletionOutcome.REVIEW_APPROVED
        assert record.review_summary == "LGTM"
        assert record.risk_level == "low"
        assert record.checks_passed == ["tests_added"]
        assert RequestedAction.ADD_CODE_REVIEWED_LABEL in record.requested_actions
        assert RequestedAction.REMOVE_NEEDS_REWORK_LABEL in record.requested_actions

    def test_build_changes_requested_record(self):
        """Test building completion record for changes_requested status."""
        args = self._make_args(
            issues="Missing tests",
            risk="high",
            checks_needed=["tests_added", "error_handling"],
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.CHANGES_REQUESTED, args)

        assert record.outcome == CompletionOutcome.REVIEW_CHANGES_REQUESTED
        assert record.review_issues == "Missing tests"
        assert record.risk_level == "high"
        assert record.checks_needed == ["tests_added", "error_handling"]
        assert RequestedAction.ADD_NEEDS_REWORK_LABEL in record.requested_actions


class TestWriteCompletionRecord:
    """Test the write_completion_record function."""

    def test_write_completion_record_creates_file(self, tmp_path):
        """Test that write_completion_record creates the JSON file."""
        # Create a fake git repo structure
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        record = CompletionRecord(
            session_id="test-session",
            timestamp="2024-01-01T12:00:00",
            outcome=CompletionOutcome.COMPLETED,
            summary="Test summary",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )

        # Change to tmp_path and write
        original_cwd = Path.cwd()
        original_completion_path = os.environ.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)
        try:
            os.chdir(tmp_path)
            output_path = write_completion_record(record)

            assert output_path.exists()
            assert str(output_path).endswith(COMPLETION_RECORD_PATH)

            # Verify JSON content
            with open(output_path) as f:
                data = json.load(f)
            assert data["session_id"] == "test-session"
            assert data["outcome"] == "completed"
        finally:
            os.chdir(original_cwd)
            if original_completion_path is not None:
                os.environ[f"{ENV_PREFIX}COMPLETION_PATH"] = original_completion_path

    def test_write_completion_record_creates_directory(self, tmp_path):
        """Test that write_completion_record creates .issue-orchestrator directory."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        record = CompletionRecord(
            session_id="test-session",
            timestamp="2024-01-01T12:00:00",
            outcome=CompletionOutcome.BLOCKED,
            summary="Test",
            requested_actions=[],
        )

        original_cwd = Path.cwd()
        original_completion_path = os.environ.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)
        try:
            os.chdir(tmp_path)
            write_completion_record(record)

            assert (tmp_path / ".issue-orchestrator").exists()
            assert (tmp_path / ".issue-orchestrator").is_dir()
        finally:
            os.chdir(original_cwd)
            if original_completion_path is not None:
                os.environ[f"{ENV_PREFIX}COMPLETION_PATH"] = original_completion_path


class TestWriteMarkerFile:
    """Test the write_marker_file function."""

    def test_write_marker_file_creates_file(self, tmp_path):
        """Test that write_marker_file creates the marker file."""
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            write_marker_file("completed")

            marker_path = tmp_path / ".agent-done-marker"
            assert marker_path.exists()

            content = marker_path.read_text()
            assert "agent-done completed called at" in content
        finally:
            os.chdir(original_cwd)


class TestCheckDirtyFiles:
    """Test check_dirty_files excludes orchestrator runtime artifacts.

    Two filter categories apply:

    - Runtime metadata (``.issue-orchestrator/`` sessions/backups,
      ``.claude/``): always filtered regardless of tracked/untracked.
    - Orchestrator-planted source
      (``src/issue_orchestrator/entrypoints/cli_tools/``): filtered **only**
      when git reports the path as untracked (status ``??``). A tracked
      modification in the orchestrator's own repo is a legitimate
      developer edit and must still fire the guard.
    """

    def test_excludes_runtime_metadata(self):
        """Session logs and .claude/ settings are excluded regardless of status."""
        porcelain = (
            "?? .issue-orchestrator/sessions/20260303__coder-81/ui-session.log\n"
            " M .claude/settings.json\n"
            " M src/app.py\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=porcelain)
            result = check_dirty_files()
        assert result == ["M src/app.py"]

    def test_returns_all_when_no_orchestrator_files(self):
        porcelain = "?? newfile.py\n M existing.py\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=porcelain)
            result = check_dirty_files()
        assert len(result) == 2

    def test_excludes_untracked_planted_cli_tools(self):
        """Untracked sync_cli_tools plantings must not block coding-done."""
        porcelain = (
            "?? src/issue_orchestrator/entrypoints/cli_tools/coding_done.py\n"
            "?? src/issue_orchestrator/entrypoints/cli_tools/reviewer_done.py\n"
            " M src/app.py\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=porcelain)
            result = check_dirty_files()
        assert result == ["M src/app.py"]

    def test_excludes_untracked_summary_src_dir(self):
        """``?? src/`` is the porcelain summary form git emits when an
        entire untracked subtree is collapsed to its topmost dir. In a
        foreign repo this is exactly how the planted ``cli_tools/`` tree
        shows up — the prior substring-based filter silently missed it.
        """
        porcelain = "?? src/\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=porcelain)
            result = check_dirty_files()
        assert result == []

    def test_keeps_tracked_modified_planted_cli_tools(self):
        """Tracked modifications to the same cli_tools files are legitimate
        developer edits in the orchestrator's own repo and must still fire
        the dirty-tree guard.
        """
        porcelain = (
            " M src/issue_orchestrator/entrypoints/cli_tools/coding_done.py\n"
            "M  src/issue_orchestrator/entrypoints/cli_tools/reviewer_done.py\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=porcelain)
            result = check_dirty_files()
        assert result == [
            "M src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
            "M  src/issue_orchestrator/entrypoints/cli_tools/reviewer_done.py",
        ]

    def test_uses_untracked_files_all_flag(self):
        """Porcelain must request per-file untracked listing so the planted
        subtree doesn't get collapsed to ``?? src/`` and evade per-file
        filters elsewhere.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="")
            check_dirty_files()
        args = mock_run.call_args[0][0]
        assert args[:2] == ["git", "status"]
        assert "--porcelain" in args
        assert "--untracked-files=all" in args

    def test_clean_tree_returns_empty(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="")
            result = check_dirty_files()
        assert result == []


class TestMain:
    """Test the split CLI entry points: coding_done_main and reviewer_done_main."""

    def test_coding_done_completed_dry_run(self, capsys):
        """Test dry run mode for completed status via coding-done."""
        with patch('sys.argv', [
            'coding-done', 'completed',
            '--implementation', 'Added feature',
            '--problems', 'None',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                coding_done_main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "completed" in captured.out

    def test_coding_done_blocked_dry_run(self, capsys):
        """Test dry run mode for blocked status via coding-done."""
        with patch('sys.argv', [
            'coding-done', 'blocked',
            '--reason', 'Need API key',
            '--attempted', 'Checked env vars',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                coding_done_main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "blocked" in captured.out

    def test_coding_done_needs_human_dry_run(self, capsys):
        """Test dry run mode for needs_human status via coding-done."""
        with patch('sys.argv', [
            'coding-done', 'needs_human',
            '--question', 'Which approach?',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                coding_done_main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "needs_human" in captured.out

    def test_reviewer_done_approved_dry_run(self, capsys):
        """Test dry run mode for approved status via reviewer-done."""
        with patch('sys.argv', [
            'reviewer-done', 'approved',
            '--summary', 'LGTM',
            '--risk', 'low',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                reviewer_done_main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "review_approved" in captured.out

    def test_reviewer_done_changes_requested_dry_run(self, capsys):
        """Test dry run mode for changes_requested status via reviewer-done."""
        with patch('sys.argv', [
            'reviewer-done', 'changes_requested',
            '--issues', 'Missing tests',
            '--risk', 'medium',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                reviewer_done_main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "review_changes_requested" in captured.out

    def test_coding_done_missing_required_field(self):
        """Test error when required field is missing for coding-done."""
        with patch('sys.argv', [
            'coding-done', 'completed',
            '--implementation', 'Added feature'
            # Missing --problems
        ]):
            with pytest.raises(SystemExit):
                coding_done_main()

    def test_coding_done_invalid_status(self):
        """Test error with invalid status for coding-done."""
        with patch('sys.argv', ['coding-done', 'invalid']):
            with pytest.raises(SystemExit):
                coding_done_main()

    def test_coding_done_help_flag(self):
        """Test --help flag displays usage for coding-done."""
        with patch('sys.argv', ['coding-done', '--help']):
            with pytest.raises(SystemExit) as exc_info:
                coding_done_main()
            # argparse exits with 0 for --help
            assert exc_info.value.code == 0

    def test_coding_done_no_args(self):
        """Test error when no arguments provided to coding-done."""
        with patch('sys.argv', ['coding-done']):
            with pytest.raises(SystemExit):
                coding_done_main()

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.run_preflight_push_check', return_value=(True, None, None))
    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.load_validation_cmd', return_value=(None, None))
    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_coding_done_writes_completion_record(self, _mock_dirty, _mock_val, _mock_push, tmp_path):
        """Test that coding-done writes completion record to file."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        original_cwd = Path.cwd()
        original_completion_path = os.environ.pop(f"{ENV_PREFIX}COMPLETION_PATH", None)
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'coding-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    coding_done_main()

            # Check file was written
            record_path = tmp_path / COMPLETION_RECORD_PATH
            assert record_path.exists()

            with open(record_path) as f:
                data = json.load(f)
            assert data["session_id"] == "test-123"
            assert data["outcome"] == "completed"
            assert data["implementation"] == "Added feature"
        finally:
            os.chdir(original_cwd)
            if original_completion_path is not None:
                os.environ[f"{ENV_PREFIX}COMPLETION_PATH"] = original_completion_path

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.run_preflight_push_check', return_value=(True, None, None))
    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_coding_done_writes_marker_file(self, _mock_dirty, _mock_push, tmp_path):
        """Test that coding-done writes marker file."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'coding-done', 'blocked',
                '--reason', 'Need API',
                '--attempted', 'Checked env',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    coding_done_main()

            # Check marker file was written
            marker_path = tmp_path / ".agent-done-marker"
            assert marker_path.exists()
            content = marker_path.read_text()
            assert "agent-done blocked called at" in content
        finally:
            os.chdir(original_cwd)


class TestShortFlags:
    """Test short flag versions for coding-done."""

    def test_short_flags_completed(self, capsys):
        """Test using short flags for completed status via coding-done."""
        with patch('sys.argv', [
            'coding-done', 'completed',
            '-i', 'Implementation text',
            '-p', 'No problems',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                coding_done_main()

                captured = capsys.readouterr()
                assert "Implementation text" in captured.out

    def test_short_flags_blocked(self, capsys):
        """Test using short flags for blocked status via coding-done."""
        with patch('sys.argv', [
            'coding-done', 'blocked',
            '-r', 'Reason text',
            '-a', 'Attempted text',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                coding_done_main()

                captured = capsys.readouterr()
                assert "Reason text" in captured.out

    def test_short_flags_needs_human(self, capsys):
        """Test using short flags for needs_human status via coding-done."""
        with patch('sys.argv', [
            'coding-done', 'needs_human',
            '-q', 'Question text',
            '-c', 'Context text',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                coding_done_main()

                captured = capsys.readouterr()
                assert "Question text" in captured.out


class TestCompletionRecordSerialization:
    """Test that completion records can be serialized and deserialized."""

    def test_roundtrip_completed(self):
        """Test serialization roundtrip for completed record."""
        args = argparse.Namespace(
            implementation="Added feature X",
            problems="None encountered",
            reason=None,
            attempted=None,
            blocked_by=None,
            when_unblocked=None,
            question=None,
            context=None,
            options=None,
            default=None,
            summary=None,
            issues=None,
            risk=None,
            checks=None,
            checks_needed=None,
        )

        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.COMPLETED, args)

        # Serialize and deserialize
        data = record.to_dict()
        restored = CompletionRecord.from_dict(data)

        assert restored.session_id == record.session_id
        assert restored.outcome == record.outcome
        assert restored.implementation == record.implementation
        assert restored.problems == record.problems
        assert restored.follow_up_issues is None
        assert len(restored.requested_actions) == len(record.requested_actions)

    def test_roundtrip_blocked(self):
        """Test serialization roundtrip for blocked record."""
        args = argparse.Namespace(
            implementation=None,
            problems=None,
            reason="Waiting for API",
            attempted="Checked docs",
            blocked_by=[123, 456],
            when_unblocked="Implement after API ready",
            question=None,
            context=None,
            options=None,
            default=None,
            summary=None,
            issues=None,
            risk=None,
            checks=None,
            checks_needed=None,
        )

        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.BLOCKED, args)

        data = record.to_dict()
        restored = CompletionRecord.from_dict(data)

        assert restored.blocked_reason == record.blocked_reason
        assert restored.blocked_by == record.blocked_by
        assert restored.when_unblocked == record.when_unblocked

    def test_roundtrip_completed_with_follow_up_issues(self):
        record = CompletionRecord(
            session_id="test-session",
            timestamp="2024-01-01T12:00:00",
            outcome=CompletionOutcome.COMPLETED,
            summary="Implemented the main issue",
            requested_actions=[RequestedAction.PUSH_BRANCH],
            implementation="Implemented the main issue",
            problems="None",
            follow_up_issues=[
                ProposedFollowUpIssue(
                    title="Create flaky test follow-up",
                    reason="A flaky test was discovered while validating the assigned issue.",
                    evidence="tests/unit/test_logging.py",
                    suggested_labels=["bug", "tests"],
                    blocking=False,
                )
            ],
        )

        restored = CompletionRecord.from_dict(record.to_dict())

        assert restored.follow_up_issues == record.follow_up_issues


class TestLoadFollowUpIssues:
    def test_load_follow_up_issues_from_json_array(self, tmp_path: Path):
        path = tmp_path / "followups.json"
        path.write_text(json.dumps([
            {"title": "Issue A", "reason": "Reason A", "blocking": False},
            {"title": "Issue B", "reason": "Reason B", "evidence": "file.py:12"},
        ]), encoding="utf-8")

        loaded = load_follow_up_issues(str(path))

        assert loaded == [
            ProposedFollowUpIssue(title="Issue A", reason="Reason A", blocking=False),
            ProposedFollowUpIssue(title="Issue B", reason="Reason B", evidence="file.py:12", blocking=False),
        ]

    def test_load_follow_up_issues_from_jsonl(self, tmp_path: Path):
        path = tmp_path / "followups.jsonl"
        path.write_text(
            json.dumps({"title": "Issue A", "reason": "Reason A"}) + "\n"
            + json.dumps({"title": "Issue B", "reason": "Reason B", "suggested_labels": ["bug"]}) + "\n",
            encoding="utf-8",
        )

        loaded = load_follow_up_issues(str(path))

        assert loaded == [
            ProposedFollowUpIssue(title="Issue A", reason="Reason A", blocking=False),
            ProposedFollowUpIssue(title="Issue B", reason="Reason B", suggested_labels=["bug"], blocking=False),
        ]

    def test_load_follow_up_issues_rejects_invalid_entry(self, tmp_path: Path):
        path = tmp_path / "followups.json"
        path.write_text(json.dumps([{"title": "", "reason": "missing title"}]), encoding="utf-8")

        with pytest.raises(ValueError, match="follow_up_issues entries require non-empty title"):
            load_follow_up_issues(str(path))


def test_orchestrator_env_isolation_strips_unprefixed_aliases():
    """The autouse env-isolation fixture must strip unprefixed ORCHESTRATOR_*
    aliases, not only ISSUE_ORCHESTRATOR_* names.

    Regression for the validation-gate breakage: the orchestrator exports both
    ISSUE_ORCHESTRATOR_SESSION_ID and the unprefixed ORCHESTRATOR_SESSION_ID.
    coding-done's ``_is_managed_session()`` reads the unprefixed alias, so a
    leaked ORCHESTRATOR_SESSION_ID flips it into orchestrator-managed mode. When
    the unit suite runs inside an orchestrator session (coding-done's own
    validation gate), the TestAgentGateIntegration cases below then hit the
    run-dir contract and fail. The fixture in tests/conftest.py must therefore
    leave no ORCHESTRATOR_* var visible to a test.
    """
    leaked = sorted(name for name in os.environ if name.startswith("ORCHESTRATOR_"))
    assert leaked == [], f"orchestrator env aliases leaked into the test env: {leaked}"


class TestAgentGateIntegration:
    """Test agent gate validation integration in coding-done."""

    def test_record_validation_artifacts_records_junit_from_validation_config(
        self, tmp_path: Path
    ):
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            """
validation:
  quick:
    cmd: "make validate"
  junit_xml_paths:
    - reports/*.xml
""",
            encoding="utf-8",
        )
        junit_path = tmp_path / "reports" / "junit.xml"
        junit_path.parent.mkdir()
        junit_path.write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="validation" tests="1">
  <testcase classname="tests.e2e.test_smoke" name="test_smoke" time="0.01" />
</testsuite>
""",
            encoding="utf-8",
        )
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "test-123")
        stdout_path = run.run_dir / "validation-stdout.log"
        stderr_path = run.run_dir / "validation-stderr.log"
        stdout_path.write_text("ok", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        record = ValidationRecord(
            schema_version=1,
            suite="agent_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make validate",
            started_at="2026-05-07T00:00:00Z",
            ended_at="2026-05-07T00:00:01Z",
            stdout_path=str(stdout_path.relative_to(tmp_path)),
            stderr_path=str(stderr_path.relative_to(tmp_path)),
        )
        (run.run_dir / "validation-record.json").write_text(
            json.dumps(record.to_dict()),
            encoding="utf-8",
        )

        record_validation_artifacts(
            tmp_path,
            run.validation_artifacts,
            AgentGateResult(
                passed=True,
                reason="Validation passed",
                record=record,
                record_path=str(run.run_dir / "validation-record.json"),
            ),
        )

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        assert manifest["validation_status"] == "passed"
        assert any(
            artifact.get("kind") == "junit_xml"
            and artifact.get("path") == str(junit_path.resolve())
            for artifact in manifest["artifacts"].values()
        )

    def test_record_validation_artifacts_returns_none_without_record(
        self, tmp_path: Path
    ):
        run = FileSystemSessionOutput().start_run(tmp_path, "test-123")

        record_path = record_validation_artifacts(
            tmp_path,
            run.validation_artifacts,
            AgentGateResult(
                passed=True,
                reason="Validation passed",
                record=None,
                record_path=None,
            ),
        )

        manifest = FileSystemSessionOutput().read_manifest(run.run_dir)
        assert record_path is None
        assert manifest is not None
        assert "validation_record_path" not in manifest

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_agent_gate_runs_when_configured(self, _mock_dirty, tmp_path, capsys):
        """Test that agent gate validation runs when configured via coding-done."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with passing validation
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  quick:
    cmd: "echo 'ok'"
    timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'coding-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
                '--verbose'
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    coding_done_main()

            captured = capsys.readouterr()
            assert "Validation passed" in captured.out
        finally:
            os.chdir(original_cwd)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_validation_failure_exits_with_error(self, _mock_dirty, tmp_path, capsys):
        """Test that validation failure exits with error and writes diagnostics."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with failing validation
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  quick:
    cmd: "exit 1"
    timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'coding-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
                '--verbose'
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    with pytest.raises(SystemExit) as exc_info:
                        coding_done_main()
                    # Should exit with error code 1
                    assert exc_info.value.code == 1

            captured = capsys.readouterr()
            # Should print error about failure
            assert "Validation failed" in captured.out
            # Should NOT write completion record (exited before that)
            assert "Completion record written to" not in captured.out
            # Should write validation details to session output
            manifest_path = (
                tmp_path
                / ".issue-orchestrator"
                / "sessions"
                / "test-123"
                / "manifest.json"
            )
            assert manifest_path.exists()
            manifest = json.loads(manifest_path.read_text())
            validation_record_path = manifest.get("validation_record_path")
            assert validation_record_path
            assert str(validation_record_path).endswith("validation-record.json")
            assert manifest.get("validation_status") == "failed"
        finally:
            os.chdir(original_cwd)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_validation_failure_shows_stderr_inline(self, _mock_dirty, tmp_path, capsys):
        """Test that validation failure shows the actual error output inline.

        This verifies Claude can see what failed without reading separate files.
        """
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with validation that outputs error to stderr
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        # This command outputs to stderr and exits with error
        config_path.write_text("""
validation:
  quick:
    cmd: "echo 'FAILED test_something.py::test_case - AssertionError' >&2 && exit 1"
    timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'coding-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    with pytest.raises(SystemExit) as exc_info:
                        coding_done_main()
                    assert exc_info.value.code == 1

            captured = capsys.readouterr()

            # Verify the new output format
            assert "VALIDATION FAILED" in captured.out
            assert "coding-done cannot complete" in captured.out
            assert "--- STDERR (what failed) ---" in captured.out
            assert "FAILED test_something.py::test_case - AssertionError" in captured.out
            assert "--- END STDERR ---" in captured.out
            assert "TO FIX:" in captured.out
            assert 'coding-done blocked --reason "Validation failing:' in captured.out
        finally:
            os.chdir(original_cwd)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_validation_uses_selected_config_name_env(self, _mock_dirty, tmp_path, capsys):
        """coding-done should honor ISSUE_ORCHESTRATOR_CONFIG_NAME (e.g., main.yaml)."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create ONLY main.yaml (no default.yaml). Validation must still run.
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "main.yaml").write_text(
            """
validation:
  quick:
    cmd: "exit 1"
    timeout_seconds: 10
"""
        )

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch.dict(os.environ, {f"{ENV_PREFIX}CONFIG_NAME": "main.yaml"}, clear=False):
                with patch(
                    "sys.argv",
                    [
                        "coding-done",
                        "completed",
                        "--implementation",
                        "Added feature",
                        "--problems",
                        "None",
                    ],
                ):
                    with patch(
                        "issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id",
                        return_value="test-123",
                    ):
                        with pytest.raises(SystemExit) as exc_info:
                            coding_done_main()
                        # Validation should run and fail
                        assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "VALIDATION FAILED" in captured.out
            manifest_path = (
                tmp_path / ".issue-orchestrator" / "sessions" / "test-123" / "manifest.json"
            )
            assert manifest_path.exists()
            manifest = json.loads(manifest_path.read_text())
            validation_record_path = manifest.get("validation_record_path")
            assert validation_record_path
            assert str(validation_record_path).endswith("validation-record.json")
            assert manifest.get("validation_status") == "failed"
        finally:
            os.chdir(original_cwd)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_missing_selected_config_name_fails_fast(self, _mock_dirty, tmp_path, capsys):
        """coding-done should fail loudly when selected config file is missing."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Only default exists; selected config points to missing main.yaml.
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            """
validation:
  quick:
    cmd: "echo ok"
    timeout_seconds: 10
"""
        )

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch.dict(os.environ, {f"{ENV_PREFIX}CONFIG_NAME": "main.yaml"}, clear=False):
                with patch(
                    "sys.argv",
                    [
                        "coding-done",
                        "completed",
                        "--implementation",
                        "Added feature",
                        "--problems",
                        "None",
                    ],
                ):
                    with pytest.raises(SystemExit) as exc_info:
                        coding_done_main()
                    assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "Configured file 'main.yaml' not found under" in captured.err
        finally:
            os.chdir(original_cwd)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_blocked_status_skips_validation(self, _mock_dirty, tmp_path, capsys):
        """Test that blocked status skips validation entirely via coding-done.

        This is important because if tests fail and agent can't fix them,
        they should be able to report blocked without validation blocking them.
        """
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with validation that would fail
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  quick:
    cmd: "exit 1"
    timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'coding-done', 'blocked',
                '--reason', 'Tests failing and cannot fix',
                '--attempted', 'Tried multiple fixes',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    # Should NOT raise - blocked skips validation
                    coding_done_main()

            captured = capsys.readouterr()
            # Should indicate validation was skipped
            assert "Skipping validation for 'blocked' status" in captured.out
            # Should still write completion record
            assert "Completion record written to" in captured.out
        finally:
            os.chdir(original_cwd)


class TestFindWorktreeRoot:
    """Test find_worktree_root with ISSUE_ORCHESTRATOR_WORKTREE guard."""

    def test_returns_git_root_when_no_env_var(self, tmp_path):
        """Without env var, falls back to filesystem .git detection."""
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src" / "app"
        subdir.mkdir(parents=True)
        original_cwd = Path.cwd()
        try:
            os.chdir(subdir)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(f"{ENV_PREFIX}WORKTREE", None)
                result = find_worktree_root()
            assert result == tmp_path
        finally:
            os.chdir(original_cwd)

    def test_returns_cwd_when_no_git_and_no_env(self, tmp_path):
        """Without .git and without env var, returns CWD."""
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(f"{ENV_PREFIX}WORKTREE", None)
                result = find_worktree_root()
            assert result == tmp_path
        finally:
            os.chdir(original_cwd)

    def test_passes_when_cwd_matches_env_var(self, tmp_path):
        """When CWD matches WORKTREE env var, returns normally."""
        (tmp_path / ".git").mkdir()
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, {f"{ENV_PREFIX}WORKTREE": str(tmp_path)}):
                result = find_worktree_root()
            assert result == tmp_path
        finally:
            os.chdir(original_cwd)

    def test_raises_when_cwd_mismatches_env_var(self, tmp_path):
        """When CWD is in a different worktree, raises WorktreeMismatchError."""
        # Simulate two worktrees
        correct_wt = tmp_path / "correct-worktree"
        correct_wt.mkdir()
        (correct_wt / ".git").mkdir()

        wrong_wt = tmp_path / "wrong-worktree"
        wrong_wt.mkdir()
        (wrong_wt / ".git").mkdir()

        original_cwd = Path.cwd()
        try:
            os.chdir(wrong_wt)
            with patch.dict(os.environ, {f"{ENV_PREFIX}WORKTREE": str(correct_wt)}):
                with pytest.raises(WorktreeMismatchError, match="WORKTREE MISMATCH"):
                    find_worktree_root()
        finally:
            os.chdir(original_cwd)

    def test_raises_includes_both_paths_in_message(self, tmp_path):
        """Error message includes both the actual and expected paths."""
        correct_wt = tmp_path / "expected"
        correct_wt.mkdir()
        (correct_wt / ".git").mkdir()

        wrong_wt = tmp_path / "actual"
        wrong_wt.mkdir()
        (wrong_wt / ".git").mkdir()

        original_cwd = Path.cwd()
        try:
            os.chdir(wrong_wt)
            with patch.dict(os.environ, {f"{ENV_PREFIX}WORKTREE": str(correct_wt)}):
                with pytest.raises(WorktreeMismatchError, match="actual.*expected"):
                    find_worktree_root()
        finally:
            os.chdir(original_cwd)

    def test_subdirectory_of_correct_worktree_passes(self, tmp_path):
        """Agent in a subdirectory of the correct worktree should pass."""
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src" / "deep" / "nested"
        subdir.mkdir(parents=True)
        original_cwd = Path.cwd()
        try:
            os.chdir(subdir)
            with patch.dict(os.environ, {f"{ENV_PREFIX}WORKTREE": str(tmp_path)}):
                result = find_worktree_root()
            assert result == tmp_path
        finally:
            os.chdir(original_cwd)


class TestOrchestratorModeSkips:
    """Test coding-done behavior under orchestrator: validation runs, preflight push skipped."""

    def _setup_git_repo(self, tmp_path):
        """Create a minimal git repo for coding-done tests."""
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
        (tmp_path / "README.md").write_text("test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True, check=True)

    def _setup_config_with_failing_validation(self, tmp_path):
        """Create config with a validation command that fails."""
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 1'\n    timeout_seconds: 10\n"
        )
        return FileSystemSessionOutput().start_run(tmp_path, "test-123").run_dir

    def _setup_config_with_passing_validation(self, tmp_path):
        """Create config with a validation command that passes."""
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 0'\n    timeout_seconds: 10\n"
        )
        return FileSystemSessionOutput().start_run(tmp_path, "test-123").run_dir

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_runs_validation(self, _mock_dirty, tmp_path, capsys):
        """Under orchestrator, validation runs for fast agent feedback."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_failing_validation(tmp_path)

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                    '--verbose',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        with pytest.raises(SystemExit) as exc_info:
                            coding_done_main()
                        assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "VALIDATION FAILED" in captured.out
            assert "Skipping validation" not in captured.out
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_skips_preflight_push(self, _mock_dirty, tmp_path, capsys):
        """Under orchestrator, preflight push check is skipped."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                    '--verbose',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        with patch('issue_orchestrator.entrypoints.cli_tools.coding_done.run_preflight_push_check') as mock_push:
                            coding_done_main()

            captured = capsys.readouterr()
            assert "Skipping push preflight" in captured.out
            mock_push.assert_not_called()
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_still_writes_completion_record(self, _mock_dirty, tmp_path):
        """Under orchestrator, completion record is written after validation passes."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        coding_done_main()

            record_path = tmp_path / COMPLETION_RECORD_PATH
            assert record_path.exists()

            data = json.loads(record_path.read_text())
            assert data["session_id"] == "test-123"
            assert data["outcome"] == "completed"
            assert data["implementation"] == "Added feature"
            assert data["validation_record_path"] == str(
                run_dir / "validation-record.json"
            )
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_does_not_write_dangling_validation_record_path(
        self,
        _mock_dirty,
        tmp_path,
    ):
        """Completion records only point at validation records that exist."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with patch(
                        'issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id',
                        return_value='test-123',
                    ):
                        with patch(
                            'issue_orchestrator.entrypoints.cli_tools.coding_done.run_validation',
                            return_value=AgentGateResult(
                                passed=True,
                                reason="Validation passed",
                                record=None,
                                record_path=None,
                            ),
                        ):
                            coding_done_main()

            completion_record = json.loads(
                (tmp_path / COMPLETION_RECORD_PATH).read_text()
            )
            assert completion_record.get("validation_record_path") is None
            assert not (run_dir / "validation-record.json").exists()
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_prefixed_session_id_also_triggers_orchestrator_mode(self, _mock_dirty, tmp_path, capsys):
        """ISSUE_ORCHESTRATOR_SESSION_ID (prefixed) also triggers orchestrator mode for push skip."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)
        prefixed = f"{ENV_PREFIX}SESSION_ID"

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(
                os.environ,
                {prefixed: "test-123", f"{ENV_PREFIX}RUN_DIR": str(run_dir)},
            ):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                    '--verbose',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        coding_done_main()

            captured = capsys.readouterr()
            assert "Skipping push preflight" in captured.out
            assert "Skipping validation" not in captured.out
        finally:
            os.chdir(original_cwd)
            os.environ.pop(prefixed, None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_produces_validation_record(self, _mock_dirty, tmp_path):
        """Under orchestrator, validation-record.json is produced for review exchange.

        This is the critical integration point: the review exchange loop checks
        for validation-record.json in the coder's run directory. If coding-done
        skips validation under orchestrator, the review exchange fails with
        coder_protocol_violation.
        """
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        coding_done_main()

            # Verify validation-record.json was written to the session output dir
            validation_record = run_dir / "validation-record.json"
            assert validation_record.exists(), (
                "validation-record.json must be produced under orchestrator mode "
                "so the review exchange loop can verify the coder ran validation"
            )
            data = json.loads(validation_record.read_text())
            assert data["passed"] is True
            completion_record = json.loads((tmp_path / COMPLETION_RECORD_PATH).read_text())
            assert completion_record["validation_record_path"] == str(validation_record)
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_requires_explicit_run_dir(self, _mock_dirty, tmp_path, capsys):
        """Orchestrated validation must use the owner-injected run directory."""
        self._setup_git_repo(tmp_path)
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 0'\n    timeout_seconds: 10\n"
        )

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, {"ORCHESTRATOR_SESSION_ID": "orch-session-1"}):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with pytest.raises(SystemExit) as exc_info:
                        coding_done_main()
                    assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "ISSUE_ORCHESTRATOR_RUN_DIR is required" in captured.err
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    @patch('issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files', return_value=[])
    def test_orchestrator_mode_rejects_run_dir_for_different_session(
        self,
        _mock_dirty,
        tmp_path,
        capsys,
    ):
        """The injected run directory must belong to the completing session."""
        self._setup_git_repo(tmp_path)
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 0'\n    timeout_seconds: 10\n"
        )
        run_dir = FileSystemSessionOutput().start_run(
            tmp_path,
            "other-session",
        ).run_dir

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with patch(
                        'issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id',
                        return_value='test-123',
                    ):
                        with pytest.raises(SystemExit) as exc_info:
                            coding_done_main()
                        assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "ISSUE_ORCHESTRATOR_RUN_DIR belongs to 'other-session'" in captured.err
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)


class TestPostValidationDirtyRecheck:
    """Cover the post-validation dirty re-check that closes the temporal
    variance with the orchestrator's publish gate.

    Motivation: ``validate.sh`` can write to the tree (auto-formatters,
    generated artifacts, integration-test side effects). Without a
    second dirty check the agent completes "successfully" while the
    orchestrator's later check finds dirty files and silently rejects
    the push, producing the rework loop seen on tixmeup issue #359.
    """

    def _setup_git_repo(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
        (tmp_path / "README.md").write_text("test")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True, check=True)

    def _setup_config_with_passing_validation(self, tmp_path):
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  quick:\n    cmd: 'exit 0'\n    timeout_seconds: 10\n"
        )
        return FileSystemSessionOutput().start_run(tmp_path, "test-123").run_dir

    def test_post_validation_dirty_files_block_completion(self, tmp_path, capsys):
        """If validation modifies tracked files, coding-done must reject."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)

        # First call (pre-validation): clean. Second call (post-validation):
        # validate.sh has dirtied a tracked test file — exactly the 359 shape.
        dirty_call_returns = [
            [],
            ["M  inventory-impl/src/test/kotlin/.../JdbcTest.kt"],
        ]

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        with patch(
                            'issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files',
                            side_effect=dirty_call_returns,
                        ):
                            with pytest.raises(SystemExit) as exc_info:
                                coding_done_main()
                            assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "WORKING TREE WAS DIRTIED BY VALIDATION" in captured.out
            assert "JdbcTest.kt" in captured.out
            assert "Validation modified the working tree" in captured.out

            # Completion record must NOT have been written
            record_path = tmp_path / COMPLETION_RECORD_PATH
            assert not record_path.exists(), (
                "Post-validation dirty rejection must abort before writing the "
                "completion record — otherwise the orchestrator picks up a "
                "stale record claiming success."
            )
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)

    def test_post_validation_clean_completes_normally(self, tmp_path):
        """When validation does not dirty the tree, coding-done completes."""
        self._setup_git_repo(tmp_path)
        run_dir = self._setup_config_with_passing_validation(tmp_path)

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with patch.dict(os.environ, _orchestrator_env(run_dir)):
                with patch('sys.argv', [
                    'coding-done', 'completed',
                    '--implementation', 'Added feature',
                    '--problems', 'None',
                ]):
                    with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                        with patch(
                            'issue_orchestrator.entrypoints.cli_tools.coding_done.check_dirty_files',
                            return_value=[],
                        ) as mock_dirty:
                            coding_done_main()

                            # Both checks must have run — pre- and post-validation
                            assert mock_dirty.call_count == 2, (
                                "Both pre- and post-validation dirty checks "
                                "must run when validation succeeds."
                            )

            record_path = tmp_path / COMPLETION_RECORD_PATH
            assert record_path.exists()
        finally:
            os.chdir(original_cwd)
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)
