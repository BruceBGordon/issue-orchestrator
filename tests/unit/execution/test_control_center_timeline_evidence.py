"""Behavior-owner tests for repository-scoped Control Center evidence access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from issue_orchestrator.domain.run_manifest import RunManifest
from issue_orchestrator.domain.timeline_evidence import (
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
    TimelineEvidenceStatus,
)
from issue_orchestrator.execution.control_center_timeline_evidence import (
    ControlCenterTimelineEvidenceAccess,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.e2e_worktree import get_e2e_worktree_path
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.timeline_store import TimelineRecord


def _config(repo_root: Path) -> Config:
    config = Config(repo_root=repo_root)
    config.session_output_retention_days = 7
    config.session_output_retention_tier = "hot"
    return config


def _access(repo_root: Path, calls: list[tuple[Path, object]]):
    supervisor = object()

    def load_config(root: Path, actual_supervisor: object) -> Config:
        calls.append((root, actual_supervisor))
        return _config(repo_root)

    return ControlCenterTimelineEvidenceAccess(
        get_supervisor=lambda: supervisor,
        load_config=load_config,
    ), supervisor


def _append_run(database: Path, issue_number: int, run_dir: Path) -> None:
    with SqliteTimelineStore(database) as store:
        store.append(
            issue_number,
            TimelineRecord(
                event_id=f"event-{issue_number}",
                timestamp="2026-01-01T00:00:00Z",
                event="session.completed",
                source_event="session.completed",
                data={"run_dir": str(run_dir)},
            ),
        )


def test_open_issue_falls_back_to_e2e_timeline_and_uses_effective_config(
    tmp_path: Path,
) -> None:
    base_database = state_dir(tmp_path) / "timeline.sqlite"
    with SqliteTimelineStore(base_database):
        pass
    e2e_database = state_dir(get_e2e_worktree_path(tmp_path)) / "timeline.sqlite"
    _append_run(e2e_database, 42, tmp_path / "run-42")
    calls: list[tuple[Path, object]] = []
    access, supervisor = _access(tmp_path, calls)

    with access.open_issue(tmp_path, 42) as issue:
        assert issue is not None
        assert [record.event_id for record in issue.records] == ["event-42"]

    assert calls == [(tmp_path, supervisor)]


def test_pin_and_describe_share_exact_issue_run_owner(tmp_path: Path) -> None:
    run = FileSystemSessionOutput().start_run(
        tmp_path / "worktree",
        "issue-42",
        issue_number=42,
    )
    manifest = RunManifest.load(run.run_dir)
    manifest.ended_at = datetime.now(timezone.utc).isoformat()
    manifest.retention_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=7)
    ).isoformat()
    manifest.evidence_available = True
    manifest.save()
    database = state_dir(tmp_path) / "timeline.sqlite"
    _append_run(database, 42, run.run_dir)
    access, _ = _access(tmp_path, [])
    command = SetTimelineEvidencePinCommand(
        identity=TimelineEvidenceIdentity(42, run.run_dir),
        pinned=True,
    )

    pinned = access.set_pinned(tmp_path, command)
    described = access.describe(tmp_path, command.identity)

    assert pinned.status is TimelineEvidenceStatus.PINNED
    assert described.status is TimelineEvidenceStatus.PINNED
    assert RunManifest.load(run.run_dir).retention_pinned is True


def test_unknown_run_fails_closed_as_missing(tmp_path: Path) -> None:
    database = state_dir(tmp_path) / "timeline.sqlite"
    _append_run(database, 42, tmp_path / "known-run")
    access, _ = _access(tmp_path, [])

    state = access.describe(
        tmp_path,
        TimelineEvidenceIdentity(42, tmp_path / "other-run"),
    )

    assert state.status is TimelineEvidenceStatus.MISSING
    assert state.available is False
