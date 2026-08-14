"""Tests for web manifest endpoint session-context resolution."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints import web
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.timeline_evidence import NULL_TIMELINE_EVIDENCE


def test_session_manifest_uses_history_worktree_path(tmp_path, monkeypatch) -> None:
    issue_number = 2641
    worktree_path = tmp_path / f"repo-{issue_number}"
    worktree_path.mkdir(parents=True, exist_ok=True)

    session_output = FileSystemSessionOutput()
    run = session_output.start_run(
        worktree_path=worktree_path,
        session_name=f"issue-{issue_number}",
        issue_number=issue_number,
    )

    history_entry = SimpleNamespace(
        issue_number=issue_number,
        worktree_path=str(worktree_path),
    )
    state = SimpleNamespace(active_sessions=[], session_history=[history_entry])
    config = SimpleNamespace(
        worktree_base=tmp_path,
        repo="owner/repo",
        repo_root=tmp_path / "repo",
    )
    dummy = SimpleNamespace(
        state=state,
        config=config,
        deps=SimpleNamespace(timeline_evidence=NULL_TIMELINE_EVIDENCE),
    )
    monkeypatch.setattr(web, "_orchestrator", dummy)

    client = TestClient(web.app)
    response = client.get(f"/api/session/manifest/{issue_number}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["manifest"]["run_dir"] == str(run.run_dir)
