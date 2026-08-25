from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.execution.manifest_accessor import (
    ArtifactNotFoundError,
    ManifestAccessor,
    RunIdentity,
    worktree_path_from_run_dir,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def _build_accessor(tmp_path: Path, *, issue_number: int = 123) -> tuple[ManifestAccessor, Path, Path]:
    session_output = FileSystemSessionOutput()
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    run = session_output.start_run(worktree, f"issue-{issue_number}", issue_number=issue_number)
    identity = RunIdentity(issue_number=issue_number, run_dir=run.run_dir)
    return ManifestAccessor(identity), worktree, run.run_dir


def test_worktree_path_from_run_dir_requires_canonical_session_namespace(
    tmp_path: Path,
) -> None:
    canonical_run = tmp_path / ".issue-orchestrator" / "sessions" / "run"
    canonical_run.mkdir(parents=True)
    lookalike_run = tmp_path / ".issue-orchestrator" / "not-sessions" / "run"
    lookalike_run.mkdir(parents=True)

    assert worktree_path_from_run_dir(canonical_run) == tmp_path
    assert worktree_path_from_run_dir(lookalike_run) is None


def test_get_agent_log_returns_run_scoped_log(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text('{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n', encoding="utf-8")

    artifact = accessor.get_terminal_recording()
    assert artifact.descriptor.artifact_type == "terminal_recording"
    assert artifact.path == recording
    assert artifact.descriptor.length_bytes is not None


def test_get_terminal_recording_returns_run_scoped_recording(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text('{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n', encoding="utf-8")

    artifact = accessor.get_terminal_recording()
    assert artifact.descriptor.artifact_type == "terminal_recording"
    assert artifact.path == recording


def test_get_terminal_recording_raises_when_empty(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "terminal-recording.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_terminal_recording()


def test_get_review_exchange_phase_terminal_recording_returns_phase_scoped_recording(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "review-exchange" / "round-002" / "reviewer" / "terminal-recording.jsonl"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(round_index=2, role="reviewer")
    assert artifact.path == recording


def test_get_review_exchange_phase_terminal_recording_raises_when_missing(tmp_path: Path) -> None:
    accessor, _worktree, _run_dir = _build_accessor(tmp_path)

    with pytest.raises(ArtifactNotFoundError, match="review exchange recording not found"):
        accessor.get_review_exchange_phase_terminal_recording(round_index=1, role="reviewer")


def test_get_review_exchange_phase_terminal_recording_resolves_persistent_layout(
    tmp_path: Path,
) -> None:
    """Persistent runner writes one continuous recording per role at
    ``<run_dir>/<role>/terminal-recording.jsonl`` (no per-round subdir).
    The accessor must serve that file when present so timeline review
    actions don't 404 after the cutover (#6160 review feedback).
    """
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    persistent_recording = run_dir / "reviewer" / "terminal-recording.jsonl"
    persistent_recording.parent.mkdir(parents=True, exist_ok=True)
    persistent_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=2, role="reviewer",
    )
    # The persistent layout has no per-round directory; the chapter
    # offsets in chapters.json are how the UI scrubs to a specific
    # round inside the role's continuous recording.
    assert artifact.path == persistent_recording


def test_get_review_exchange_phase_terminal_recording_prefers_persistent_when_both_exist(
    tmp_path: Path,
) -> None:
    """When both layouts are present (e.g. mid-migration), the
    persistent layout wins — that's the live one the new runner writes."""
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    persistent_recording = run_dir / "coder" / "terminal-recording.jsonl"
    persistent_recording.parent.mkdir(parents=True, exist_ok=True)
    persistent_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"bmV3","schema_version":1}\n',
        encoding="utf-8",
    )
    legacy_recording = (
        run_dir / "review-exchange" / "round-001" / "coder" / "terminal-recording.jsonl"
    )
    legacy_recording.parent.mkdir(parents=True, exist_ok=True)
    legacy_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"b2xk","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=1, role="coder",
    )
    assert artifact.path == persistent_recording


def test_get_review_exchange_phase_terminal_recording_falls_back_to_legacy_layout(
    tmp_path: Path,
) -> None:
    """Pre-cutover runs that wrote the spawn-per-phase layout must
    still resolve. The fallback path keeps existing artifacts viewable
    after the cutover lands."""
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    legacy_recording = (
        run_dir / "review-exchange" / "round-003" / "reviewer" / "terminal-recording.jsonl"
    )
    legacy_recording.parent.mkdir(parents=True, exist_ok=True)
    legacy_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"bGVnYWN5","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=3, role="reviewer",
    )
    assert artifact.path == legacy_recording


def test_get_review_artifact_requires_persisted_turn_artifact_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    turns = run_dir / "review-exchange" / "turns"
    turns.mkdir(parents=True)
    report = turns / "round-001.reviewer.attempt-001.review-report.md"
    report.write_text("# Review\n\nLooks good.\n", encoding="utf-8")

    artifact = accessor.get_review_artifact(
        artifact_path=str(report),
        artifact_type="review_report",
    )

    assert artifact.path == report
    assert artifact.descriptor.content_type == "text/markdown"


def test_get_review_artifact_rejects_client_selected_non_turn_file(
    tmp_path: Path,
) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    stray = run_dir / "anything-not-emitted-by-this-run.md"
    stray.write_text("# Not a review artifact\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="not a persisted review turn artifact"):
        accessor.get_review_artifact(
            artifact_path=str(stray),
            artifact_type="review_report",
        )


def test_get_review_artifact_rejects_wrong_filename_for_type(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    turns = run_dir / "review-exchange" / "turns"
    turns.mkdir(parents=True)
    report = turns / "round-001.reviewer.attempt-001.not-a-review.md"
    report.write_text("# Not a review artifact\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="not a persisted review turn artifact"):
        accessor.get_review_artifact(
            artifact_path=str(report),
            artifact_type="review_report",
        )


def test_get_review_artifact_serves_the_tech_lead_pair(tmp_path: Path) -> None:
    """The tech lead's report/decision pair reads through the SAME run-scoped
    policy the reviewer's pair does (#6858 F4), so the dashboard needs no second
    artifact endpoint and no second containment rule."""
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    data_dir = run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True)
    (data_dir / "tech-lead-report.md").write_text("# Health review\n", encoding="utf-8")
    (data_dir / "tech-lead-decision.json").write_text('{"summary": "ok"}', encoding="utf-8")

    report = accessor.get_review_artifact(
        artifact_path="tech-lead-data/tech-lead-report.md",
        artifact_type="tech_lead_report",
    )
    decision = accessor.get_review_artifact(
        artifact_path="tech-lead-data/tech-lead-decision.json",
        artifact_type="tech_lead_decision",
    )

    assert report.descriptor.content_type == "text/markdown"
    assert decision.descriptor.content_type == "application/json"


def test_get_review_artifact_rejects_a_tech_lead_path_outside_its_directory(
    tmp_path: Path,
) -> None:
    """A caller-supplied path is checked against the type's declared home."""
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    stray = run_dir / "tech-lead-report.md"
    stray.write_text("# Not where the run writes it\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="not a persisted tech-lead artifact"):
        accessor.get_review_artifact(
            artifact_path=str(stray),
            artifact_type="tech_lead_report",
        )


def test_get_review_artifact_rejects_a_tech_lead_path_escaping_the_run(
    tmp_path: Path,
) -> None:
    accessor, _worktree, _run_dir = _build_accessor(tmp_path)
    outside = tmp_path / "elsewhere" / "tech-lead-report.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# Someone else's run\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="escapes run_dir"):
        accessor.get_review_artifact(
            artifact_path=str(outside),
            artifact_type="tech_lead_report",
        )


def test_get_review_artifact_rejects_a_tech_lead_decision_that_is_not_json(
    tmp_path: Path,
) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    data_dir = run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True)
    (data_dir / "tech-lead-decision.json").write_text("not json", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError):
        accessor.get_review_artifact(
            artifact_path="tech-lead-data/tech-lead-decision.json",
            artifact_type="tech_lead_decision",
        )


def test_get_agent_log_uses_terminal_recording_even_when_claude_log_exists(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text('{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n', encoding="utf-8")
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_log = claude_dir / "run.jsonl"
    claude_log.write_text('{"message":"ok"}\n', encoding="utf-8")

    FileSystemSessionOutput().update_manifest(
        run_dir,
        {
            "claude_log_dir": str(claude_dir),
        },
    )

    artifact = accessor.get_agent_log()
    assert artifact.path == recording
    assert artifact.path != claude_log


def test_get_agent_log_raises_when_terminal_recording_empty(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "terminal-recording.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_agent_log()


def test_get_agent_log_allow_empty_returns_terminal_recording(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("", encoding="utf-8")

    artifact = accessor.get_agent_log(allow_empty=True)
    assert artifact.path == recording


def test_get_agent_log_raises_when_terminal_recording_only_empty_by_default(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_agent_log()


def test_get_agent_log_raises_when_terminal_recording_empty_even_if_claude_log_exists(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("", encoding="utf-8")
    claude = run_dir / "claude.jsonl"
    claude.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\n', encoding="utf-8")
    FileSystemSessionOutput().update_manifest(
        run_dir,
        {
            "claude_log_path": str(claude),
        },
    )

    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_agent_log()


def test_get_claude_log_reads_manifest_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    claude = run_dir / "claude.jsonl"
    claude.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "claude_log_path": str(claude),
            }
        ),
        encoding="utf-8",
    )

    artifact = accessor.get_claude_log()
    assert artifact.descriptor.artifact_type == "claude_log"
    assert artifact.path == claude


def test_get_claude_log_does_not_discover_later_file_for_unbound_run(
    tmp_path: Path,
) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "claude_log_dir": str(claude_dir),
            }
        ),
        encoding="utf-8",
    )
    manifest_path = run_dir / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    later = claude_dir / "later.jsonl"
    later.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"later"}]}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ArtifactNotFoundError, match="manifest missing claude_log_path"):
        accessor.get_claude_log()

    assert manifest_path.read_bytes() == manifest_before


def test_get_completion_record_uses_worktree_relative_manifest_path(tmp_path: Path) -> None:
    accessor, worktree, run_dir = _build_accessor(tmp_path)
    completion_rel = ".issue-orchestrator/sessions/issue-123/completion-backend.json"
    completion = worktree / completion_rel
    completion.parent.mkdir(parents=True, exist_ok=True)
    completion.write_text('{"status":"completed"}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "completion_path": completion_rel,
            }
        ),
        encoding="utf-8",
    )

    artifact = accessor.get_completion_record()
    assert artifact.descriptor.artifact_type == "completion_record"
    assert artifact.path == completion
    assert artifact.descriptor.content_type == "application/json"


def test_get_validation_record_raises_when_missing(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "validation_record_path": "validation.json",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactNotFoundError):
        accessor.get_validation_record()


def _write_manifest_prompt_path(run_dir: Path, session_prompt_path: str) -> None:
    """Set ``session_prompt_path`` in the run's manifest for prompt tests."""
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "session_prompt_path": session_prompt_path,
            }
        ),
        encoding="utf-8",
    )


def test_get_session_prompt_prefers_manifest_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    launch = run_dir / "session-prompt.txt"
    launch.write_text("launch prompt\n", encoding="utf-8")
    _write_manifest_prompt_path(run_dir, str(launch))

    artifact = accessor.get_session_prompt()
    assert artifact.descriptor.artifact_type == "session_prompt"
    assert artifact.path == launch


def test_get_session_prompt_falls_back_to_session_prompt_txt(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    prompt = run_dir / "session-prompt.txt"
    prompt.write_text("fallback prompt\n", encoding="utf-8")

    artifact = accessor.get_session_prompt()
    assert artifact.path == prompt


def test_get_session_prompt_falls_back_to_retry_prompt(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    retry = run_dir / "retry-prompt.md"
    retry.write_text("retry prompt\n", encoding="utf-8")

    artifact = accessor.get_session_prompt()
    assert artifact.path == retry


def test_get_session_prompt_falls_back_to_review_exchange_prompt(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    coder_prompt = run_dir / "review-exchange" / "round-001" / "coder-prompt.txt"
    coder_prompt.parent.mkdir(parents=True, exist_ok=True)
    coder_prompt.write_text("exchange coder prompt\n", encoding="utf-8")

    artifact = accessor.get_session_prompt()
    assert artifact.path == coder_prompt


def test_get_session_prompt_skips_empty_candidate(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "session-prompt.txt").write_text("", encoding="utf-8")
    retry = run_dir / "retry-prompt.md"
    retry.write_text("retry prompt\n", encoding="utf-8")

    artifact = accessor.get_session_prompt()
    assert artifact.path == retry


def test_get_session_prompt_rejects_absolute_outside_manifest_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    _write_manifest_prompt_path(run_dir, str(outside))

    # The only candidate escapes run_dir and there is no in-run fallback, so
    # the absolute out-of-run file is never returned.
    with pytest.raises(ArtifactNotFoundError):
        accessor.get_session_prompt()


def test_get_session_prompt_rejects_relative_escape_manifest_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    outside = run_dir.parent / "outside.txt"
    outside.write_text("ESCAPED\n", encoding="utf-8")
    _write_manifest_prompt_path(run_dir, "../outside.txt")

    with pytest.raises(ArtifactNotFoundError):
        accessor.get_session_prompt()


def test_get_session_prompt_skips_escape_and_serves_in_run_fallback(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("SHOULD NOT SERVE\n", encoding="utf-8")
    _write_manifest_prompt_path(run_dir, str(outside))
    fallback = run_dir / "session-prompt.txt"
    fallback.write_text("in-run fallback\n", encoding="utf-8")

    # A stale/malformed manifest that escapes run_dir is skipped, not fatal:
    # the run's own contained prompt is served and the escape content stays out.
    artifact = accessor.get_session_prompt()
    assert artifact.path == fallback
    assert artifact.path.read_text(encoding="utf-8") == "in-run fallback\n"
