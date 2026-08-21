"""Truthful local-artifact fixtures for Timeline route and browser tests."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path


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
) -> Path:
    """Rewrite and materialize one captured local-artifact path."""
    if not original_value.strip():
        raise ValueError(f"captured Timeline field {field.value} is empty")

    if field is TimelineFixturePathField.WORKTREE_PATH:
        rewritten_path = run_dir.parent
        rewritten_path.mkdir(parents=True, exist_ok=True)
        return rewritten_path

    rewritten_path = run_dir / Path(original_value).name
    rewritten_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "{}\n"
        if field is TimelineFixturePathField.COMPLETION_PATH_ABSOLUTE
        else "fixture prompt\n"
    )
    rewritten_path.write_text(content, encoding="utf-8")
    return rewritten_path


def write_available_timeline_run_manifest(
    *,
    run_dir: Path,
    terminal_recording: Path,
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

    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": run_dir.name,
                "run_id": run_dir.name,
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
