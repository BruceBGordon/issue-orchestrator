"""Reporting support for an ENTIRELY ABSENT completion record.

Sibling of :mod:`.invalid_completion_record`, which covers the other half of
the same problem: a record that is present but rejected. Both cases need the
same forensic context — where the record was expected, whether the agent-done
marker exists, what completion-shaped files are lying around — so the context
builder lives here and the invalid-record reporter consumes it.

Kept out of ``SessionController`` deliberately: none of this decides anything.
It gathers filesystem facts and writes them somewhere a human can read them.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..infra.logging_config import issue_log
from ..infra.validation_state import _truncate_with_tail
from ..observation.observation import SessionObservationResult
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)

# Written by the completion CLI tools right before they exit. Its presence with
# no completion record narrows the diagnosis to "the agent tried and the write
# failed" rather than "the agent never finished".
AGENT_DONE_MARKER = ".agent-done-marker"


def write_no_completion_diagnostic(
    *,
    observation: SessionObservationResult,
    worktree_path: Path,
    issue_number: int,
    session_name: str,
    run_dir: Path,
    completion_path: str | None,
    session_output: SessionOutput,
    debug_context: dict[str, Any] | None = None,
) -> None:
    """Persist a durable diagnostic snapshot when completion is missing."""
    try:
        requested_rel_path = (
            completion_path or ".issue-orchestrator/completion.json"
        )
        requested_path = (worktree_path / requested_rel_path).resolve()

        run_dir_completion_path: str | None = None
        run_dir_completion_exists: bool | None = None
        run_dir_completion_size: int | None = None
        if completion_path:
            completion_name = Path(completion_path).name
            run_dir_candidate = run_dir / completion_name
            run_dir_completion_path = str(run_dir_candidate)
            run_dir_completion_exists = run_dir_candidate.exists()
            if run_dir_completion_exists:
                run_dir_completion_size = run_dir_candidate.stat().st_size

        requested_exists = requested_path.exists()
        requested_size = requested_path.stat().st_size if requested_exists else None

        diagnostic = {
            "kind": "no-completion-record",
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "issue_number": issue_number,
            "session_name": session_name,
            "observation": observation.observation.value,
            "runtime_minutes": observation.runtime_minutes,
            "requested_completion_path": requested_rel_path,
            "requested_completion_abs_path": str(requested_path),
            "requested_completion_exists": requested_exists,
            "requested_completion_size": requested_size,
            "run_dir": str(run_dir),
            "run_dir_completion_abs_path": run_dir_completion_path,
            "run_dir_completion_exists": run_dir_completion_exists,
            "run_dir_completion_size": run_dir_completion_size,
            "pid": os.getpid(),
        }
        if debug_context:
            diagnostic.update(debug_context)
        diagnostic_path = session_output.write_diagnostic(
            run_dir,
            diagnostic,
            prefix="no-completion",
        )
        logger.info(
            issue_log(
                issue_number,
                "Saved no-completion diagnostic: session=%s path=%s",
            ),
            session_name,
            diagnostic_path,
        )
    except Exception as exc:
        logger.warning(
            issue_log(
                issue_number,
                "Failed to write no-completion diagnostic for session=%s: %s",
            ),
            session_name,
            exc,
        )

def collect_completion_debug_context(
    *,
    worktree_path: Path,
    run_dir: Path,
    completion_path: str | None,
) -> dict[str, Any]:
    requested_rel_path = completion_path or ".issue-orchestrator/completion.json"
    requested_path = (worktree_path / requested_rel_path).resolve()
    marker_path = worktree_path / AGENT_DONE_MARKER
    marker_exists = marker_path.exists()
    marker_preview: str | None = None
    if marker_exists:
        try:
            marker_preview = _truncate_with_tail(
                marker_path.read_text(encoding="utf-8"), 200
            )
        except OSError:
            marker_preview = "<unreadable>"
    return {
        "requested_completion_path": requested_rel_path,
        "requested_completion_abs_path": str(requested_path),
        "agent_done_marker_path": str(marker_path.resolve()),
        "agent_done_marker_exists": marker_exists,
        "agent_done_marker_preview": marker_preview,
        "nearby_completion_candidates": _find_nearby_completion_candidates(
            worktree_path=worktree_path,
            run_dir=run_dir,
            requested_path=requested_path,
        ),
    }

def _find_nearby_completion_candidates(
    *,
    worktree_path: Path,
    run_dir: Path,
    requested_path: Path,
) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    root_candidates = worktree_path / ".issue-orchestrator"
    if root_candidates.exists():
        candidates.extend(root_candidates.glob("completion*.json"))
        sessions_dir = root_candidates / "sessions"
        if sessions_dir.exists():
            candidates.extend(sessions_dir.glob("**/completion*.json"))

    unique_paths: dict[Path, None] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == requested_path:
            continue
        unique_paths[resolved] = None

    sorted_candidates = sorted(
        unique_paths.keys(),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )[:10]
    records: list[dict[str, Any]] = []
    for candidate in sorted_candidates:
        try:
            stat = candidate.stat()
            relative_to_run_dir = None
            try:
                relative_to_run_dir = str(candidate.relative_to(run_dir))
            except ValueError:
                relative_to_run_dir = None
            records.append(
                {
                    "path": str(candidate),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                    "under_run_dir": relative_to_run_dir is not None,
                    "run_dir_relative_path": relative_to_run_dir,
                }
            )
        except OSError:
            continue
    return records

def log_completion_debug_context(
    issue_number: int,
    session_name: str,
    debug_context: dict[str, Any],
) -> None:
    logger.warning(
        issue_log(
            issue_number,
            "Completion debug: session=%s marker_path=%s marker_exists=%s marker_preview=%s",
        ),
        session_name,
        debug_context["agent_done_marker_path"],
        debug_context["agent_done_marker_exists"],
        debug_context["agent_done_marker_preview"] or "",
    )
    nearby_candidates = debug_context["nearby_completion_candidates"]
    if nearby_candidates:
        logger.warning(
            issue_log(
                issue_number, "Completion debug: session=%s nearby_candidates=%s"
            ),
            session_name,
            nearby_candidates,
        )
    else:
        logger.warning(
            issue_log(
                issue_number, "Completion debug: session=%s nearby_candidates=[]"
            ),
            session_name,
        )


__all__ = [
    "AGENT_DONE_MARKER",
    "collect_completion_debug_context",
    "log_completion_debug_context",
    "write_no_completion_diagnostic",
]
