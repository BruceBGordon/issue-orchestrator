from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable

from fastapi import Depends, FastAPI, Request

from ..infra.orchestrator import Orchestrator
from ..execution.manifest_accessor import worktree_path_from_run_dir
from ..ports.review_artifact_reader import ReviewArtifactReader

_WEB_SESSION_CONTEXT_GETTER_STATE_KEY = "web_session_context_get_orchestrator"
_WEB_REVIEW_ARTIFACT_READER_STATE_KEY = "web_session_context_review_artifact_reader"


@dataclass(frozen=True)
class IssueSessionContext:
    """Resolved latest session context for an issue across active/history/storage."""

    worktree_path: Path | None = None
    session_name: str | None = None
    run_dir: Path | None = None


def install_web_session_context_dependencies(
    app: FastAPI,
    *,
    get_orchestrator: Callable[[], Orchestrator | None],
    review_artifact_reader: ReviewArtifactReader,
) -> None:
    """Install web session-context dependencies on the FastAPI app."""
    setattr(app.state, _WEB_SESSION_CONTEXT_GETTER_STATE_KEY, get_orchestrator)
    setattr(app.state, _WEB_REVIEW_ARTIFACT_READER_STATE_KEY, review_artifact_reader)


def get_web_orchestrator(request: Request) -> Orchestrator | None:
    """Return the configured web orchestrator, if available."""
    getter = getattr(request.app.state, _WEB_SESSION_CONTEXT_GETTER_STATE_KEY, None)
    if getter is None:
        return None
    return getter()


WebOrchestratorDependency = Annotated[
    Orchestrator | None,
    Depends(get_web_orchestrator),
]


def get_web_review_artifact_reader(request: Request) -> ReviewArtifactReader:
    """Return the configured review-artifact command reader."""
    reader = getattr(request.app.state, _WEB_REVIEW_ARTIFACT_READER_STATE_KEY, None)
    if reader is None:
        raise RuntimeError("review artifact reader dependency is not installed")
    return reader


ReviewArtifactReaderDependency = Annotated[
    ReviewArtifactReader,
    Depends(get_web_review_artifact_reader),
]


def _latest_session_history_entry(
    orchestrator: Orchestrator | None,
    issue_number: int,
) -> Any | None:
    """Return the most recent history entry for an issue."""
    if not orchestrator:
        return None
    for entry in reversed(orchestrator.state.session_history):
        if entry.issue_number == issue_number:
            return entry
    return None


def resolve_issue_session_context(
    orchestrator: Orchestrator | None,
    issue_number: int,
) -> IssueSessionContext:
    """Resolve current issue session context from active or local history."""
    if not orchestrator:
        return IssueSessionContext()

    for session in orchestrator.state.active_sessions:
        if session.issue.number == issue_number:
            return IssueSessionContext(
                worktree_path=session.worktree_path,
                session_name=session.terminal_id,
                run_dir=session.run_assets.run_dir,
            )

    from ..execution.session_output_adapter import FileSystemSessionOutput

    session_output = FileSystemSessionOutput()

    history_entry = _latest_session_history_entry(orchestrator, issue_number)
    if history_entry:
        worktree_value = getattr(history_entry, "worktree_path", None)
        worktree_path = Path(worktree_value) if worktree_value else None
        run_dir = None
        if worktree_path:
            run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
        session_name = session_output.session_name_from_path(str(run_dir)) if run_dir else None
        return IssueSessionContext(
            worktree_path=worktree_path,
            session_name=session_name,
            run_dir=run_dir,
        )

    # Fail-fast: do not scan sibling worktrees/repos for session state.
    return IssueSessionContext()


def issue_title_for(
    orchestrator: Orchestrator | None,
    issue_number: int,
) -> str:
    """Resolve the best-known title for an issue from active and persisted state."""
    if not orchestrator:
        return f"Issue #{issue_number}"
    for session in orchestrator.state.active_sessions:
        if session.issue.number == issue_number:
            return session.issue.title
    for issue in orchestrator.state.cached_queue_issues:
        if issue.number == issue_number:
            return issue.title
    for entry in reversed(orchestrator.state.session_history):
        if entry.issue_number == issue_number:
            return entry.title
    return f"Issue #{issue_number}"


__all__ = [
    "IssueSessionContext",
    "ReviewArtifactReaderDependency",
    "WebOrchestratorDependency",
    "get_web_review_artifact_reader",
    "get_web_orchestrator",
    "install_web_session_context_dependencies",
    "issue_title_for",
    "resolve_issue_session_context",
    "worktree_path_from_run_dir",
]
