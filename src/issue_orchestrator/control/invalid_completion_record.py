"""Reporting support for present but rejected completion records."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_run_scoped_event
from ..ports.session_output import SessionOutput
from .completion_record_validation import CompletionRecordLoadResult
from ..observation.observation import SessionObservationResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvalidCompletionRecordDecision:
    reason: str
    completion_detail: dict[str, Any]
    diagnostic_path: str | None


def report_invalid_completion_record(
    *,
    observation: SessionObservationResult,
    worktree_path: Path,
    issue_number: int,
    session_name: str,
    run_dir: Path,
    completion_path: str | None,
    load_result: CompletionRecordLoadResult,
    debug_context: dict[str, Any],
    session_output: SessionOutput,
    events: EventSink,
) -> InvalidCompletionRecordDecision:
    """Persist diagnostics, emit a timeline event, and return decision detail."""
    error = load_result.error or "Completion record rejected"
    failure = load_result.failure.value if load_result.failure else "unknown"
    summary = f"Completion record rejected: {error}"
    diagnostic_path = _write_diagnostic(
        observation=observation,
        worktree_path=worktree_path,
        issue_number=issue_number,
        session_name=session_name,
        run_dir=run_dir,
        completion_path=completion_path,
        load_result=load_result,
        debug_context=debug_context,
        session_output=session_output,
    )
    completion_abs_path = str(load_result.path.resolve())
    payload: dict[str, Any] = {
        "issue_number": issue_number,
        "session_name": session_name,
        "observation": observation.observation.value,
        "summary": summary,
        "reason": summary,
        "error": summary,
        "completion_load_failure": failure,
        "completion_parse_error": error,
        "completion_path": completion_path,
        "completion_path_absolute": completion_abs_path,
        "completion_file_exists": load_result.exists,
        "completion_file_size": load_result.size,
        "run_dir": str(run_dir),
    }
    if diagnostic_path is not None:
        diagnostic_value = str(diagnostic_path)
        payload["diagnostic_path"] = diagnostic_value
        payload["artifacts"] = [
            {
                "type": "diagnostic",
                "label": "Invalid Completion Diagnostic",
                "value": diagnostic_value,
            }
        ]
    events.publish(
        make_run_scoped_event(EventName.SESSION_INVALID_COMPLETION_RECORD, payload)
    )
    logger.error(
        issue_log(
            issue_number,
            "SESSION COMPLETE: status=FAILED outcome=none "
            "reason=invalid_completion_record session=%s expected_completion=%s "
            "failure=%s error=%s",
        ),
        session_name,
        completion_abs_path,
        failure,
        error,
    )
    diagnostic_value = str(diagnostic_path) if diagnostic_path else None
    return InvalidCompletionRecordDecision(
        reason=summary,
        completion_detail={
            "failure_kind": "invalid_completion_record",
            "failure_reason": summary,
            "completion_load_failure": failure,
            "completion_parse_error": error,
            "completion_path_absolute": completion_abs_path,
            "diagnostic_path": diagnostic_value,
        },
        diagnostic_path=diagnostic_value,
    )


def _write_diagnostic(
    *,
    observation: SessionObservationResult,
    worktree_path: Path,
    issue_number: int,
    session_name: str,
    run_dir: Path,
    completion_path: str | None,
    load_result: CompletionRecordLoadResult,
    debug_context: dict[str, Any],
    session_output: SessionOutput,
) -> Path | None:
    """Persist a durable diagnostic snapshot for rejected completion JSON."""
    try:
        requested_rel_path = completion_path or ".issue-orchestrator/completion.json"
        requested_path = (worktree_path / requested_rel_path).resolve()
        diagnostic = {
            "kind": "invalid-completion-record",
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "issue_number": issue_number,
            "session_name": session_name,
            "observation": observation.observation.value,
            "runtime_minutes": observation.runtime_minutes,
            "requested_completion_path": requested_rel_path,
            "requested_completion_abs_path": str(requested_path),
            "requested_completion_exists": load_result.exists,
            "requested_completion_size": load_result.size,
            "completion_load_failure": (
                load_result.failure.value if load_result.failure else None
            ),
            "completion_parse_error": load_result.error or "Completion record rejected",
            "run_dir": str(run_dir),
            "pid": os.getpid(),
        }
        diagnostic.update(debug_context)
        diagnostic_path = session_output.write_diagnostic(
            run_dir,
            diagnostic,
            prefix="invalid-completion",
        )
        logger.info(
            issue_log(
                issue_number,
                "Saved invalid-completion diagnostic: session=%s path=%s",
            ),
            session_name,
            diagnostic_path,
        )
        return diagnostic_path
    except Exception as exc:
        logger.warning(
            issue_log(
                issue_number,
                "Failed to write invalid-completion diagnostic for session=%s: %s",
            ),
            session_name,
            exc,
        )
        return None
