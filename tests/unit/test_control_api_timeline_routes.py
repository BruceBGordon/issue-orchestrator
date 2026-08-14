"""Retention policy tests for standalone Control Center Timeline routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from issue_orchestrator.domain.timeline_evidence import (
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
)
from issue_orchestrator.entrypoints.control_api_timeline_routes import (
    control_timeline_router,
)
from issue_orchestrator.entrypoints.control_api_timeline_support import (
    ControlApiTimelineDependencies,
    install_control_api_timeline_dependencies,
)


def _state(
    issue_number: int,
    run_dir: Path,
    *,
    status: TimelineEvidenceStatus,
    available: bool,
) -> TimelineEvidenceState:
    return TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(issue_number, run_dir),
        status=status,
        label=("Evidence retained" if available else "Evidence expired"),
        available=available,
        pinned=False,
        archived=True,
        help_text="Retention policy result",
    )


def _client(tmp_path: Path, access: MagicMock) -> TestClient:
    app = FastAPI()
    install_control_api_timeline_dependencies(
        app,
        ControlApiTimelineDependencies(
            timeline_evidence=access,
            validate_repo_root=lambda raw: (
                tmp_path if raw == str(tmp_path) else None
            ),
        ),
    )
    app.include_router(control_timeline_router)
    return TestClient(app)


def test_terminal_recording_rejects_expired_evidence_before_serving(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "archive" / "run-1"
    access = MagicMock()
    access.describe.return_value = _state(
        42,
        run_dir,
        status=TimelineEvidenceStatus.EXPIRED,
        available=False,
    )
    serve = MagicMock()
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.web_session_routes.serve_terminal_recording",
        serve,
    )

    response = _client(tmp_path, access).get(
        "/api/session/terminal-recording/42",
        params={"repo_root": str(tmp_path), "run_dir": str(run_dir)},
    )

    assert response.status_code == 410
    assert response.json()["error"] == "Evidence expired"
    serve.assert_not_called()


def test_terminal_recording_serves_retained_exact_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "archive" / "run-1"
    access = MagicMock()
    access.describe.return_value = _state(
        42,
        run_dir,
        status=TimelineEvidenceStatus.RETAINED,
        available=True,
    )
    serve = MagicMock(return_value=JSONResponse({"events": []}))
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.web_session_routes.serve_terminal_recording",
        serve,
    )

    response = _client(tmp_path, access).get(
        "/api/session/terminal-recording/42",
        params={"repo_root": str(tmp_path), "run_dir": str(run_dir)},
    )

    assert response.status_code == 200
    access.describe.assert_called_once_with(
        tmp_path,
        TimelineEvidenceIdentity(42, run_dir),
    )
    serve.assert_called_once_with(42, str(run_dir), 0, 200, None, None)


def test_pin_command_preserves_repository_and_exact_run_scope(tmp_path: Path) -> None:
    run_dir = tmp_path / "archive" / "run-1"
    access = MagicMock()
    access.set_pinned.return_value = TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(42, run_dir),
        status=TimelineEvidenceStatus.PINNED,
        label="Pinned",
        available=True,
        pinned=True,
        archived=True,
        help_text="Pinned evidence is retained until unpinned.",
    )

    response = _client(tmp_path, access).put(
        "/api/issues/42/timeline-evidence/pin",
        params={"repo_root": str(tmp_path)},
        json={"run_dir": str(run_dir), "pinned": True},
    )

    assert response.status_code == 200
    command = access.set_pinned.call_args.args[1]
    assert access.set_pinned.call_args.args[0] == tmp_path
    assert command.identity == TimelineEvidenceIdentity(42, run_dir)
    assert command.pinned is True
    assert response.json()["status"] == "pinned"


def test_timeline_routes_require_valid_repository_scope(tmp_path: Path) -> None:
    access = MagicMock()
    client = _client(tmp_path, access)

    response = client.get(
        "/api/session/terminal-recording/42",
        params={"repo_root": "/outside", "run_dir": "/tmp/run"},
    )

    assert response.status_code == 400
    access.describe.assert_not_called()
