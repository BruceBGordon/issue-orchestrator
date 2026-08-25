"""Test helpers for constructing typed session run assets."""

import json
from pathlib import Path
from unittest.mock import Mock

from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.session_watchdog import (
    SCHEDULED_OUTER_WATCHDOG_MANIFEST_FIELD,
    ScheduledSessionWatchdog,
)


def make_session_run_assets(
    base: Path,
    *,
    session_name: str = "issue-123",
    run_id: str = "20260603T000000000000Z",
    scheduled_watchdog_timeout_minutes: int = 90,
) -> SessionRunAssets:
    if isinstance(base, Mock) or not isinstance(base, Path):
        raise TypeError("base must be a pathlib.Path")
    if "MagicMock" in base.parts or any(part.startswith("mock.") for part in base.parts):
        raise TypeError("base must not be derived from a mock path")
    scheduled_watchdog = ScheduledSessionWatchdog(
        scheduled_watchdog_timeout_minutes
    )
    base = base.resolve()
    run_dir = (
        base / ".issue-orchestrator" / "sessions" / f"{run_id}__{session_name}"
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    log_path = run_dir / "terminal-recording.jsonl"
    started_at = "2026-06-03T00:00:00+00:00"
    manifest_path.write_text(
        json.dumps(
            {
                "session_name": session_name,
                "run_id": run_id,
                "started_at": started_at,
                "worktree": str(base),
                "run_dir": str(run_dir),
                "log_path": str(log_path),
                SCHEDULED_OUTER_WATCHDOG_MANIFEST_FIELD: (
                    scheduled_watchdog.timeout_minutes
                ),
                "artifacts": {
                    "terminal_recording": {
                        "kind": "terminal_recording",
                        "path": str(log_path),
                        "content_type": "application/x-ndjson",
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")
    return SessionRunAssets.from_paths(
        session_name=session_name,
        run_id=run_id,
        worktree_path=base,
        run_dir=run_dir,
        terminal_recording_path=log_path,
        manifest_path=manifest_path,
        started_at=started_at,
    )
