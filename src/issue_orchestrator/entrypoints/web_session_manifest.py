"""Run-manifest and Timeline-evidence access boundary for web diagnostics."""

from __future__ import annotations

import json
import logging
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from ..control.session_analyzer import load_analysis
from ..domain.run_manifest import RunManifest
from ..domain.timeline_evidence import TimelineEvidenceIdentity, TimelineEvidenceState
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..execution.validation_failure_summary import (
    load_validation_failure_summary,
    load_validation_failure_summary_with_config,
)
from ..view_models.timeline_evidence_presentation import (
    timeline_evidence_public_state,
)
from .web_session_context import (
    WebOrchestratorDependency,
    resolve_issue_session_context,
)

logger = logging.getLogger(__name__)


def timeline_evidence_state(
    orchestrator: WebOrchestratorDependency,
    issue_number: int,
    run_dir: Path,
) -> TimelineEvidenceState | None:
    """Resolve one exact run's retention state through its owner."""
    if orchestrator is None:
        raise RuntimeError("Orchestrator is required to resolve Timeline evidence")
    state = orchestrator.deps.timeline_evidence.describe(
        TimelineEvidenceIdentity(issue_number, run_dir)
    )
    return state if isinstance(state, TimelineEvidenceState) else None


def unavailable_evidence_response(
    orchestrator: WebOrchestratorDependency,
    issue_number: int,
    run_dir: Path,
) -> JSONResponse | None:
    """Fail closed when retention says an exact run is no longer available."""
    try:
        state = timeline_evidence_state(orchestrator, issue_number, run_dir)
    except (FileNotFoundError, ValueError) as exc:
        return JSONResponse(
            {"error": "Timeline evidence unavailable", "detail": str(exc)},
            status_code=404,
        )
    if state is None or state.available:
        return None
    return JSONResponse(
        {
            "error": state.label,
            "detail": state.help_text,
            "run_dir": str(state.identity.run_dir),
        },
        status_code=410,
    )


def _manifest_response(
    run_dir: Path,
    session_name: str | None,
    *,
    config: Any = None,
    include_passed_validation: bool = False,
    evidence_state: TimelineEvidenceState | None = None,
) -> JSONResponse:
    """Load a run manifest and its diagnostic projections."""
    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        missing_result: dict[str, Any] = {
            "run_dir": str(run_dir),
            "session_name": session_name,
            "manifest": None,
        }
        if evidence_state is not None:
            missing_result["timeline_evidence"] = timeline_evidence_public_state(
                evidence_state
            )
        return JSONResponse(missing_result)
    except Exception as exc:
        return JSONResponse(
            {"error": f"Failed to read manifest: {exc}"}, status_code=500
        )

    result = _manifest_payload(run_dir, session_name, manifest, evidence_state)
    _attach_session_identity(result, run_dir)
    _attach_analysis(result, run_dir)
    validation = (
        load_validation_failure_summary_with_config(
            run_dir,
            config=config,
            include_passed=include_passed_validation,
        )
        if config is not None
        else load_validation_failure_summary(
            run_dir,
            include_passed=include_passed_validation,
        )
    )
    if validation is not None:
        result["validation_failure"] = validation.to_dict()
    return JSONResponse(result)


def _manifest_payload(
    run_dir: Path,
    session_name: str | None,
    manifest: RunManifest,
    evidence_state: TimelineEvidenceState | None,
) -> MutableMapping[str, Any]:
    result: MutableMapping[str, Any] = {
        "run_dir": str(run_dir),
        "session_name": session_name,
        "manifest": manifest.to_dict(),
    }
    if evidence_state is not None:
        result["timeline_evidence"] = timeline_evidence_public_state(evidence_state)
    return result


def _attach_session_identity(
    result: MutableMapping[str, Any], run_dir: Path
) -> None:
    path = run_dir / "session-identity.json"
    if not path.exists():
        return
    try:
        result["session_identity"] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("Failed to read session identity: %s", path, exc_info=True)


def _attach_analysis(result: MutableMapping[str, Any], run_dir: Path) -> None:
    analysis = load_analysis(run_dir)
    if analysis is None:
        return
    result["analysis"] = {
        "headline": analysis.headline,
        "detail": analysis.detail,
        "suggestions": list(analysis.suggestions),
    }


def _describe_evidence(
    orchestrator: WebOrchestratorDependency,
    issue_number: int,
    run_dir: Path,
) -> tuple[TimelineEvidenceState | None, JSONResponse | None]:
    try:
        return timeline_evidence_state(orchestrator, issue_number, run_dir), None
    except (FileNotFoundError, ValueError) as exc:
        return None, JSONResponse({"error": str(exc)}, status_code=404)


def _serve_run_manifest(
    *,
    orchestrator: WebOrchestratorDependency,
    issue_number: int,
    run_dir: Path,
    session_name: str | None,
    evidence_state: TimelineEvidenceState | None,
    include_passed_validation: bool,
) -> JSONResponse:
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    session_output = FileSystemSessionOutput()
    resolved_session_name = session_name or session_output.session_name_from_path(
        str(run_dir)
    )
    state = evidence_state
    if state is None:
        state, error = _describe_evidence(orchestrator, issue_number, run_dir)
        if error is not None:
            return error
    if state is None or (not state.archived and state.available):
        session_output.attach_claude_log(run_dir)
    return _manifest_response(
        run_dir,
        resolved_session_name,
        config=orchestrator.config,
        include_passed_validation=include_passed_validation,
        evidence_state=state,
    )


def session_manifest_response(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
    *,
    include_passed_validation: bool = False,
) -> JSONResponse:
    """Build the diagnostics manifest response for an exact session run."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    context = resolve_issue_session_context(orchestrator, issue_number)
    worktree_path = context.worktree_path
    session_name = context.session_name
    resolved_run_dir = context.run_dir
    evidence_state: TimelineEvidenceState | None = None

    if run_dir:
        candidate = Path(run_dir)
        evidence_state, error = _describe_evidence(
            orchestrator, issue_number, candidate
        )
        if error is not None:
            return error
        if not candidate.exists() and evidence_state is None:
            return JSONResponse(
                {"error": "Requested session run not found", "run_dir": str(candidate)},
                status_code=404,
            )
        resolved_run_dir = candidate

    if resolved_run_dir:
        return _serve_run_manifest(
            orchestrator=orchestrator,
            issue_number=issue_number,
            run_dir=resolved_run_dir,
            session_name=session_name,
            evidence_state=evidence_state,
            include_passed_validation=include_passed_validation,
        )
    if not worktree_path:
        return JSONResponse(
            {
                "error": f"No worktree path found for issue #{issue_number}",
                "hint": "Session may have been cleaned up or never started",
            },
            status_code=404,
        )

    resolved_run_dir = FileSystemSessionOutput().find_run_dir_for_issue(
        worktree_path, issue_number
    )
    if not resolved_run_dir:
        return JSONResponse(
            {
                "error": "No session run found",
                "hint": "Session may not have started or output was removed",
            },
            status_code=404,
        )
    return _serve_run_manifest(
        orchestrator=orchestrator,
        issue_number=issue_number,
        run_dir=resolved_run_dir,
        session_name=session_name,
        evidence_state=None,
        include_passed_validation=include_passed_validation,
    )
