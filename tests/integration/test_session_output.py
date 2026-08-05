"""Integration tests for session output bundles."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.domain.models import CompletionOutcome, CompletionRecord, get_completion_path
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput as SessionOutputManager
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import PushResult
from tests.callback_endpoint_helpers import ready_callback_endpoint


class DummyLabelAdapter:
    def add_label(self, issue_number: int, label: str) -> None:
        return None

    def remove_label(self, issue_number: int, label: str) -> None:
        return None


class DummyPRAdapter:
    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> PRInfo:
        return PRInfo(
            number=1,
            title=title,
            url="https://example.com/pr/1",
            branch=head,
            body=body,
            state="open",
            labels=[],
        )

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        return "https://example.com/comment/1"

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return []

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        return []


class DummyGitAdapter:
    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult:
        return PushResult(success=True, branch="feature", remote=remote, message="ok")

    def rebase_on_branch(self, worktree: Path, target: str = "origin/main"):
        return type("RebaseResult", (), {"success": True, "message": "Rebased"})()

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        return None

    def list_branch_names(self, worktree: Path) -> list[str]:
        return ["feature"]

    def get_current_branch(self, worktree: Path) -> str | None:
        return "feature"

    def get_head_sha(self, worktree: Path) -> str:
        return "test-head-sha"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False

    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool:
        return False


def _write_completion(path: Path, record: CompletionRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict()))


def test_session_output_manifest_and_validation_pointer(tmp_path: Path) -> None:
    session_name = "issue-1"
    session_output = SessionOutputManager()
    run = session_output.start_run(
        worktree_path=tmp_path,
        session_name=session_name,
        issue_number=1,
        agent_label="agent:test",
        backend="subprocess",
        claude_log_dir=str(tmp_path / ".claude" / "projects" / "test"),
        orchestrator_log=str(tmp_path / "orchestrator.log"),
    )

    completion_path = get_completion_path(agent_name="agent:test", session_name=session_name)
    # Materialize the validation record on disk so containment passes —
    # the orchestrator only attaches records that actually exist under
    # the worktree's ``.issue-orchestrator`` tree (#6008 re-review P2).
    validation_record_abs = tmp_path / ".issue-orchestrator" / "validation" / "sha.json"
    validation_record_abs.parent.mkdir(parents=True, exist_ok=True)
    validation_record_abs.write_text("{}")
    record = CompletionRecord(
        session_id=session_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary="ok",
        validation_record_path=".issue-orchestrator/validation/sha.json",
    )
    _write_completion(tmp_path / completion_path, record)

    processor = CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=DummyLabelAdapter(),
        pr_adapter=DummyPRAdapter(),
        git_adapter=DummyGitAdapter(),
        event_bus=None,
        session_output=session_output,
    )
    result = processor.process(
        worktree=tmp_path,
        run_assets=run,
        issue_number=1,
        issue_title="Test Issue",
        completion_path=completion_path,
    )
    assert result.success is True

    manifest = json.loads((run.run_dir / "manifest.json").read_text())
    assert manifest["session_name"] == session_name
    # After the containment fix, the manifest records the copied
    # artifact path under the run dir (once copied) rather than the
    # agent-supplied relative string. Assert the file was copied and
    # the manifest points at it.
    run_dir_record = run.run_dir / "validation-record.json"
    assert run_dir_record.exists()
    assert manifest["validation_record_path"] == str(run_dir_record)
    pointer = run.run_dir / "validation-record.path"
    assert pointer.exists()
    assert pointer.read_text().strip() == str(run_dir_record)


def test_orchestrator_tail_scoped_to_run(tmp_path: Path) -> None:
    session_name = "issue-1"
    session_output = SessionOutputManager()
    run1 = session_output.start_run(tmp_path, session_name, issue_number=1)
    run2 = session_output.start_run(tmp_path, session_name, issue_number=1)

    log_path = tmp_path / "orchestrator.log"
    lines = [
        f"[SESSION_RUN_START] run_id={run1.run_id} session={session_name} issue=1",
        "[issue-1] first run message",
        f"[SESSION_RUN_START] run_id={run2.run_id} session={session_name} issue=1",
        "[issue-1] second run message",
    ]
    log_path.write_text("\n".join(lines))

    tail_path = session_output.write_orchestrator_tail(
        run_dir=run2.run_dir,
        log_path=log_path,
        issue_number=1,
        session_name=session_name,
    )
    assert tail_path is not None
    tail = tail_path.read_text()
    assert "second run message" in tail
    assert "first run message" not in tail


def test_orchestrator_tail_ignores_snapshot_list_false_matches(tmp_path: Path) -> None:
    session_name = "issue-4057"
    session_output = SessionOutputManager()
    run = session_output.start_run(tmp_path, session_name, issue_number=4057)

    log_path = tmp_path / "orchestrator.log"
    lines = [
        f"[SESSION_RUN_START] run_id={run.run_id} session={session_name} issue=4057",
        "Planner: snapshot=[4048,4057,4058] -> launching=[]",
        "[issue-4048] unrelated issue line",
        "[issue-4057] relevant issue line",
        "issue_key=owner/repo:4057 selected",
    ]
    log_path.write_text("\n".join(lines))

    tail_path = session_output.write_orchestrator_tail(
        run_dir=run.run_dir,
        log_path=log_path,
        issue_number=4057,
        session_name=session_name,
    )
    assert tail_path is not None
    tail = tail_path.read_text()
    assert "relevant issue line" in tail
    assert "issue_key=owner/repo:4057 selected" in tail
    assert "snapshot=[4048,4057,4058]" not in tail
    assert "[issue-4048] unrelated issue line" not in tail


def test_orchestrator_tail_returns_none_when_no_issue_scoped_lines(tmp_path: Path) -> None:
    session_name = "issue-4057"
    session_output = SessionOutputManager()
    run = session_output.start_run(tmp_path, session_name, issue_number=4057)

    log_path = tmp_path / "orchestrator.log"
    log_path.write_text(
        "\n".join(
            [
                "planner snapshot without scoped markers",
                "[issue-4048] unrelated issue line",
            ]
        )
    )

    tail_path = session_output.write_orchestrator_tail(
        run_dir=run.run_dir,
        log_path=log_path,
        issue_number=4057,
        session_name=session_name,
    )
    assert tail_path is None


def test_session_output_selects_claude_log(tmp_path: Path) -> None:
    session_name = "issue-2"
    claude_dir = tmp_path / ".claude" / "projects" / "test"
    claude_dir.mkdir(parents=True, exist_ok=True)
    session_output = SessionOutputManager()
    run = session_output.start_run(
        worktree_path=tmp_path,
        session_name=session_name,
        issue_number=2,
        agent_label="agent:test",
        backend="subprocess",
        claude_log_dir=str(claude_dir),
    )

    older = claude_dir / "older.jsonl"
    newer = claude_dir / "newer.jsonl"
    run_start = datetime.fromisoformat(run.started_at)

    older.write_text(
        json.dumps({
            "timestamp": (run_start + timedelta(seconds=120)).isoformat(),
            "sessionId": "older",
        })
        + "\n"
    )
    newer.write_text(
        json.dumps({
            "timestamp": (run_start + timedelta(seconds=30)).isoformat(),
            "sessionId": "newer",
        })
        + "\n"
    )

    os.utime(older, (run_start.timestamp() + 300, run_start.timestamp() + 300))
    os.utime(newer, (run_start.timestamp() + 600, run_start.timestamp() + 600))

    selected = session_output.attach_claude_log(run.run_dir)
    assert selected == newer

    manifest = json.loads((run.run_dir / "manifest.json").read_text())
    assert manifest["claude_log_path"] == str(newer)
    assert manifest["claude_session_id"] == "newer"
    assert (run.run_dir / "claude-session.jsonl").is_symlink()


def test_review_completion_writes_feedback_file(tmp_path: Path) -> None:
    """Integration test: review with changes_requested writes feedback to run directory."""
    session_name = "review-issue-1-pr-42"
    session_output = SessionOutputManager()
    run = session_output.start_run(
        worktree_path=tmp_path,
        session_name=session_name,
        issue_number=1,
        agent_label="agent:reviewer",
        backend="subprocess",
    )

    # Write a review completion with changes_requested
    completion_path = get_completion_path(agent_name="agent:reviewer", session_name=session_name)
    record = CompletionRecord(
        session_id=session_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
        summary="needs work",
        review_issues="Missing unit tests for edge cases\nError handling incomplete",
    )
    _write_completion(tmp_path / completion_path, record)

    processor = CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=DummyLabelAdapter(),
        pr_adapter=DummyPRAdapter(),
        git_adapter=DummyGitAdapter(),
        event_bus=None,
        session_output=session_output,
    )
    result = processor.process(
        worktree=tmp_path,
        run_assets=run,
        issue_number=1,
        issue_title="Test Issue",
        completion_path=completion_path,
        pr_number=42,
    )
    assert result.success is True

    # Verify the feedback file was written
    feedback_file = run.run_dir / "reviewer-feedback.json"
    assert feedback_file.exists(), "Feedback file should be created for changes_requested"

    feedback_data = json.loads(feedback_file.read_text())
    assert feedback_data["pr_number"] == 42
    assert "Missing unit tests" in feedback_data["review_issues"]
    assert "timestamp" in feedback_data


def test_feedback_file_not_written_for_approved(tmp_path: Path) -> None:
    """Integration test: approved review does not write feedback file."""
    session_name = "review-issue-2-pr-99"
    session_output = SessionOutputManager()
    run = session_output.start_run(
        worktree_path=tmp_path,
        session_name=session_name,
        issue_number=2,
        agent_label="agent:reviewer",
        backend="subprocess",
    )

    completion_path = get_completion_path(agent_name="agent:reviewer", session_name=session_name)
    record = CompletionRecord(
        session_id=session_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.REVIEW_APPROVED,
        summary="LGTM",
    )
    _write_completion(tmp_path / completion_path, record)

    processor = CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=DummyLabelAdapter(),
        pr_adapter=DummyPRAdapter(),
        git_adapter=DummyGitAdapter(),
        event_bus=None,
        session_output=session_output,
    )
    result = processor.process(
        worktree=tmp_path,
        run_assets=run,
        issue_number=2,
        issue_title="Test Issue",
        completion_path=completion_path,
        pr_number=99,
    )
    assert result.success is True

    # Verify no feedback file was written
    feedback_file = run.run_dir / "reviewer-feedback.json"
    assert not feedback_file.exists(), "No feedback file for approved reviews"


def test_feedback_file_found_across_review_runs(tmp_path: Path) -> None:
    """Integration test: feedback file can be located from later rework session."""
    session_output = SessionOutputManager()

    # First: review session writes feedback
    review_session = "review-issue-3-pr-50"
    review_run = session_output.start_run(
        worktree_path=tmp_path,
        session_name=review_session,
        issue_number=3,
    )
    feedback_file = review_run.run_dir / "reviewer-feedback.json"
    feedback_file.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pr_number": 50,
        "review_issues": "Add integration tests",
    }))

    # Second: rework session starts later
    rework_session = "rework-issue-3-pr-50-c1"
    _ = session_output.start_run(
        worktree_path=tmp_path,
        session_name=rework_session,
        issue_number=3,
    )

    # Verify we can find the review session's feedback
    found = session_output.find_run_dir(tmp_path, review_session)
    assert found is not None
    assert (found / "reviewer-feedback.json").exists()

    # The feedback content is accessible
    feedback = json.loads((found / "reviewer-feedback.json").read_text())
    assert feedback["pr_number"] == 50
    assert "integration tests" in feedback["review_issues"]
