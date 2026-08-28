"""One home for *where* the suite's learned file weights live.

Two callers need the same store — the pytest plugin that observes a
slice run and the slicer that partitions the next one — and a store
spelled in two places is a store that drifts apart. Resolution lives
here so both sides ask one function.

The history sits in the git common dir beside the lane runtime history
and the validation timings, so every worktree of a repository learns
from the same runs. Outside a repository there is nothing to share, so
the loop is inert rather than inventing a location.
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.json_file_duration_history import (
    InertFileDurationHistory,
    JsonFileDurationHistory,
)
from ..ports.file_duration_history import FileDurationHistory
from .validation_timings import resolve_git_common_dir

STORE_DIRNAME = "file-durations"


def open_file_duration_history(worktree: Path) -> FileDurationHistory:
    """The repository-shared file duration history for this worktree."""
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return InertFileDurationHistory()
    return JsonFileDurationHistory(
        (common_dir / "issue-orchestrator" / STORE_DIRNAME).resolve()
    )
