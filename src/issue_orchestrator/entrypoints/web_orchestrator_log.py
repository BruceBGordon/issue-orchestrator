"""Run-scoped orchestrator-log access for the web UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.responses import JSONResponse

from ..domain.run_manifest import RunManifest
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..infra.logging_config import get_repo_log_path
from ..infra.orchestrator import Orchestrator
from .web_session_context import (
    WebOrchestratorDependency,
    resolve_issue_session_context,
    worktree_path_from_run_dir,
)
from .web_session_manifest import (
    timeline_evidence_state,
    unavailable_evidence_response,
)


@dataclass(frozen=True)
class OrchestratorLogRun:
    """Exact run and live-worktree context needed to serve its log."""

    worktree_path: Path | None
    session_name: str | None
    run_dir: Path | None


def _resolve_log_run(
    issue_number: int,
    orchestrator: Orchestrator,
    requested_run_dir: str | None,
    session_output: FileSystemSessionOutput,
) -> OrchestratorLogRun:
    context = resolve_issue_session_context(orchestrator, issue_number)
    if requested_run_dir is None:
        return OrchestratorLogRun(
            context.worktree_path,
            context.session_name,
            context.run_dir,
        )

    run_dir = Path(requested_run_dir)
    inferred_worktree = worktree_path_from_run_dir(run_dir)
    return OrchestratorLogRun(
        inferred_worktree or context.worktree_path,
        session_output.session_name_from_path(str(run_dir)),
        run_dir,
    )


def _archived_log_response(
    issue_number: int,
    orchestrator: Orchestrator,
    run_dir: Path,
) -> JSONResponse | None:
    unavailable = unavailable_evidence_response(orchestrator, issue_number, run_dir)
    if unavailable is not None:
        return unavailable
    state = timeline_evidence_state(orchestrator, issue_number, run_dir)
    if state is None or not state.archived:
        return None

    tail_path = Path(RunManifest.load(run_dir).orchestrator_tail or "")
    try:
        tail_path.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return JSONResponse(
            {"error": "Archived orchestrator log not found"},
            status_code=404,
        )
    return JSONResponse(
        {
            "filtered_log_path": str(tail_path),
            "full_log_path": None,
            "issue_number": issue_number,
        }
    )


def _live_log_response(
    issue_number: int,
    orchestrator: Orchestrator,
    run: OrchestratorLogRun,
    session_output: FileSystemSessionOutput,
) -> JSONResponse:
    if run.worktree_path is None:
        return JSONResponse(
            {"error": f"No worktree found for issue #{issue_number}"}, status_code=404
        )

    run_dir = run.run_dir or session_output.find_run_dir_for_issue(
        run.worktree_path, issue_number
    )
    if run_dir is None:
        return JSONResponse(
            {
                "error": "Could not find session run directory",
                "worktree_path": str(run.worktree_path),
            },
            status_code=500,
        )

    session_name = run.session_name or session_output.session_name_from_path(
        str(run_dir)
    )
    if session_name is None:
        return JSONResponse(
            {
                "error": "Could not determine session name for issue log filtering",
                "worktree_path": str(run.worktree_path),
            },
            status_code=500,
        )

    log_path = get_repo_log_path(orchestrator.config.repo_root)
    if not log_path.exists():
        return JSONResponse(
            {
                "error": "Orchestrator log file not found",
                "full_log_path": str(log_path),
            },
            status_code=404,
        )

    tail_path = session_output.write_orchestrator_tail(
        run_dir,
        log_path,
        issue_number,
        session_name,
        max_lines=500,
    )
    if tail_path is None:
        return JSONResponse(
            {
                "error": (
                    f"No issue-scoped orchestrator log entries found for issue #{issue_number}"
                )
            },
            status_code=500,
        )
    return JSONResponse(
        {
            "filtered_log_path": str(tail_path),
            "full_log_path": str(log_path),
            "issue_number": issue_number,
        }
    )


def orchestrator_log_response(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None,
) -> JSONResponse:
    """Serve immutable archived tails or generate a tail for a live run."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    session_output = FileSystemSessionOutput()
    run = _resolve_log_run(issue_number, orchestrator, run_dir, session_output)
    if run.run_dir is not None:
        archived = _archived_log_response(issue_number, orchestrator, run.run_dir)
        if archived is not None:
            return archived
    return _live_log_response(issue_number, orchestrator, run, session_output)
