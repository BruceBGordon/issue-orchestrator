"""Truthful local-artifact fixtures for Timeline route and browser tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from issue_orchestrator.events.artifact_semantics import (
    CompletionPathSemantics,
    completion_path_semantics,
)


class TimelineFixturePathField(StrEnum):
    """Persisted Timeline fields that name worktree-local artifacts."""

    WORKTREE_PATH = "worktree_path"
    SESSION_PROMPT_PATH = "session_prompt_path"
    COMPLETION_PATH_ABSOLUTE = "completion_path_absolute"


def rewrite_timeline_fixture_path(
    *,
    field: TimelineFixturePathField,
    run_dir: Path,
    original_value: str,
    event_name: str,
    event_data: Mapping[str, object],
) -> Path:
    """Rewrite a captured path and materialize only asserted artifacts."""
    if not original_value.strip():
        raise ValueError(f"captured Timeline field {field.value} is empty")

    if field is TimelineFixturePathField.WORKTREE_PATH:
        rewritten_path = _timeline_fixture_worktree_path(run_dir)
        rewritten_path.mkdir(parents=True, exist_ok=True)
        return rewritten_path

    rewritten_path = run_dir / Path(original_value).name
    rewritten_path.parent.mkdir(parents=True, exist_ok=True)
    if field is TimelineFixturePathField.COMPLETION_PATH_ABSOLUTE:
        semantics = completion_path_semantics(event_name, event_data)
        if semantics is CompletionPathSemantics.EXPECTED_DESTINATION:
            return rewritten_path
        if semantics is CompletionPathSemantics.NONE:
            raise ValueError(
                "captured Timeline completion path has no event semantics: "
                f"event={event_name}"
            )
    content = (
        "{}\n"
        if field is TimelineFixturePathField.COMPLETION_PATH_ABSOLUTE
        else "fixture prompt\n"
    )
    rewritten_path.write_text(content, encoding="utf-8")
    if field is TimelineFixturePathField.COMPLETION_PATH_ABSOLUTE:
        _update_timeline_run_manifest(
            run_dir=run_dir,
            updates={"completion_path": str(rewritten_path)},
        )
    return rewritten_path


def _update_timeline_run_manifest(
    *,
    run_dir: Path,
    updates: dict[str, str],
) -> None:
    """Keep a synthetic run manifest aligned with materialized artifacts."""
    manifest_path = run_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Timeline fixture manifest must be an object: {manifest_path}")
    payload.update(updates)
    manifest_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _timeline_fixture_worktree_path(run_dir: Path) -> Path:
    sessions_dir = run_dir.parent
    orchestrator_dir = sessions_dir.parent
    if sessions_dir.name != "sessions" or orchestrator_dir.name != ".issue-orchestrator":
        raise ValueError(
            "Timeline fixture run must use the canonical worktree session namespace: "
            f"{run_dir}"
        )
    return orchestrator_dir.parent


def write_available_timeline_run_manifest(
    *,
    run_dir: Path,
    terminal_recording: Path,
    issue_number: int,
) -> None:
    """Write the minimum valid manifest for an available captured run."""
    if not terminal_recording.is_file():
        raise ValueError(
            f"Timeline fixture terminal recording does not exist: {terminal_recording}"
        )
    if terminal_recording.parent != run_dir:
        raise ValueError(
            "Timeline fixture terminal recording is outside its run directory: "
            f"run_dir={run_dir} recording={terminal_recording}"
        )

    run_id, separator, session_name = run_dir.name.partition("__")
    if not separator:
        raise ValueError(
            "Timeline fixture run directory must name its run and session: "
            f"{run_dir}"
        )
    worktree_path = _timeline_fixture_worktree_path(run_dir)

    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": session_name,
                "run_id": run_id,
                "issue_number": issue_number,
                "started_at": "2026-01-01T00:00:00+00:00",
                "worktree": str(worktree_path),
                "run_dir": str(run_dir),
                "log_path": str(terminal_recording),
                "artifacts": {
                    "terminal_recording": {
                        "kind": "terminal_recording",
                        "path": str(terminal_recording),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
