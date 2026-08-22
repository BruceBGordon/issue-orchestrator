"""Tests for typed recorded session-run lookup ownership."""

import json

from issue_orchestrator.execution.recorded_session_runs import (
    ExactRecordedRun,
    InvalidRecordedRunReference,
    RecordedRunIssueMismatch,
    RecordedRunNotFound,
    RecordedRunUnreadable,
    RecordedSessionRunLookup,
    resolve_exact_recorded_run,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def test_assets_for_exact_session_returns_typed_assets(tmp_path):
    session_output = FileSystemSessionOutput()
    worktree = tmp_path
    session_name = "debug-123"
    run = session_output.start_run(
        worktree,
        session_name,
        issue_number=123,
        agent_label="agent:web",
    )

    assets = RecordedSessionRunLookup(session_output).assets_for_exact_session(
        worktree,
        session_name,
    )

    assert assets == run
    assert assets is not None
    assert assets.run_dir == run.run_dir
    assert assets.session_name == session_name


def test_assets_for_exact_session_refuses_invalid_manifest(tmp_path):
    session_output = FileSystemSessionOutput()
    worktree = tmp_path
    session_name = "debug-123"
    run = session_output.start_run(
        worktree,
        session_name,
        issue_number=123,
        agent_label="agent:web",
    )
    (run.run_dir / "manifest.json").write_text(
        json.dumps({"session_name": session_name}),
        encoding="utf-8",
    )

    assets = RecordedSessionRunLookup(session_output).assets_for_exact_session(
        worktree,
        session_name,
    )

    assert assets is None


def test_debug_resume_target_requires_manifest_completion_path(tmp_path):
    session_output = FileSystemSessionOutput()
    worktree = tmp_path
    run = session_output.start_run(
        worktree,
        "debug-123",
        issue_number=123,
        agent_label="agent:web",
    )
    completion_path = f".issue-orchestrator/sessions/{run.run_dir.name}/completion.json"
    session_output.update_manifest(run.run_dir, {"completion_path": completion_path})

    target = RecordedSessionRunLookup(session_output).debug_resume_target(
        worktree,
        issue_number=123,
    )

    assert target is not None
    assert target.run_dir == run.run_dir
    assert target.completion_path == completion_path
    assert target.completion_file() == worktree / completion_path


def test_exact_recorded_run_resolves_only_the_manifest_owned_issue(tmp_path):
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(
        tmp_path,
        "coding-123",
        issue_number=123,
        agent_label="agent:web",
    )

    result = resolve_exact_recorded_run(str(run.run_dir), issue_number=123)

    assert isinstance(result, ExactRecordedRun)
    assert result.issue_number == 123
    assert result.run_assets == run
    assert result.manifest.issue_number == 123
    assert result.run_dir == run.run_dir.resolve()
    assert result.worktree_path == tmp_path.resolve()
    assert result.session_name == "coding-123"
    assert result.artifacts.run_identity.issue_number == 123
    assert result.artifacts.run_identity.run_dir == run.run_dir.resolve()


def test_exact_recorded_run_rejects_issue_mismatch(tmp_path):
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(
        tmp_path,
        "coding-123",
        issue_number=123,
        agent_label="agent:web",
    )

    result = resolve_exact_recorded_run(str(run.run_dir), issue_number=456)

    assert result == RecordedRunIssueMismatch(
        expected_issue_number=456,
        actual_issue_number=123,
    )


def test_exact_recorded_run_rejects_lookalike_directory(tmp_path):
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(
        tmp_path,
        "coding-123",
        issue_number=123,
        agent_label="agent:web",
    )
    lookalike = tmp_path / ".issue-orchestrator" / "not-sessions" / run.run_dir.name
    lookalike.mkdir(parents=True)
    manifest = json.loads((run.run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["run_dir"] = str(lookalike)
    manifest["log_path"] = str(lookalike / "terminal-recording.jsonl")
    (lookalike / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    result = resolve_exact_recorded_run(str(lookalike), issue_number=123)

    assert isinstance(result, RecordedRunUnreadable)
    assert "run_dir must live under worktree session artifacts" in result.detail


def test_exact_recorded_run_rejects_relative_reference() -> None:
    result = resolve_exact_recorded_run("relative/run", issue_number=123)

    assert result == InvalidRecordedRunReference(detail="run_dir must be absolute")


def test_exact_recorded_run_does_not_fallback_when_requested_run_is_missing(
    tmp_path,
):
    missing = tmp_path / ".issue-orchestrator" / "sessions" / "missing"

    result = resolve_exact_recorded_run(str(missing), issue_number=123)

    assert result == RecordedRunNotFound(
        detail=f"Requested run directory not found: {missing}"
    )
