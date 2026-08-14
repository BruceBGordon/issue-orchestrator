"""Tests for durable Timeline evidence ownership."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.run_manifest import RunManifest
from issue_orchestrator.domain.timeline_evidence import (
    FinalizeTimelineEvidenceCommand,
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
    TimelineEvidenceStatus,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.timeline_evidence import FileSystemTimelineEvidence
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from issue_orchestrator.ports.timeline_store import TimelineRecord


def _terminal_run(
    worktree: Path,
    *,
    issue_number: int,
    expires_at: datetime,
) -> Path:
    output = FileSystemSessionOutput()
    assets = output.start_run(
        worktree,
        f"issue-{issue_number}",
        issue_number=issue_number,
        retention_days=7,
    )
    (assets.run_dir / "completion.json").write_text('{"status": "failed"}\n')
    manifest = RunManifest.load(assets.run_dir)
    manifest.ended_at = (expires_at - timedelta(days=7)).isoformat()
    manifest.outcome = "failed"
    manifest.retention_expires_at = expires_at.isoformat()
    manifest.evidence_available = True
    manifest.completion_path = str(assets.run_dir / "completion.json")
    manifest.save()
    return assets.run_dir


def _timeline_store_with_run(
    db_path: Path,
    *,
    issue_number: int,
    run_dir: Path,
) -> SqliteTimelineStore:
    store = SqliteTimelineStore(db_path)
    store.append(
        issue_number,
        TimelineRecord(
            event_id=f"run-{run_dir.name}",
            timestamp="2026-08-13T12:00:00+00:00",
            event="session.failed",
            source_event="session.failed",
            data={"run_dir": str(run_dir)},
        ),
    )
    return store


def test_archive_rewrites_manifest_and_timeline_paths_before_cleanup(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    worktree = tmp_path / "worktree"
    run_dir = _terminal_run(
        worktree, issue_number=42, expires_at=now + timedelta(days=7)
    )
    store = SqliteTimelineStore(tmp_path / "timeline.sqlite")
    store.append(
        42,
        TimelineRecord(
            event_id="run-event",
            timestamp=now.isoformat(),
            event="session.completed",
            source_event="session.completed",
            data={
                "run_dir": str(run_dir),
                "completion_path_absolute": str(run_dir / "completion.json"),
            },
        ),
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=store,
        now=lambda: now,
    )

    assert owner.archive_worktree(42, worktree) == 1

    archived = owner.archive_root / "42" / run_dir.name
    assert (archived / "completion.json").is_file()
    archived_manifest = RunManifest.load(archived)
    assert archived_manifest.run_dir == archived
    assert archived_manifest.completion_path == str(archived / "completion.json")
    record = store.read(42)[0]
    assert record.data["run_dir"] == str(archived)
    assert record.data["completion_path_absolute"] == str(archived / "completion.json")
    assert (
        owner.describe(TimelineEvidenceIdentity(42, archived)).status
        is TimelineEvidenceStatus.RETAINED
    )


def test_terminal_finalization_uses_owner_policy_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ended_at = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    output = FileSystemSessionOutput()
    run = output.start_run(
        tmp_path / "worktree",
        "issue-42",
        issue_number=42,
        retention_days=7,
        retention_tier="hot",
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=SqliteTimelineStore(tmp_path / "timeline.sqlite"),
        retention_days=21,
        retention_tier="cold",
        now=lambda: ended_at,
    )
    identity = TimelineEvidenceIdentity(42, run.run_dir)

    state = owner.finalize_terminal(
        FinalizeTimelineEvidenceCommand(
            identity=identity,
            outcome="failed",
            ended_at=ended_at.isoformat(),
        )
    )
    original = RunManifest.load(run.run_dir)

    assert state.status is TimelineEvidenceStatus.RETAINED
    assert original.retention_days == 21
    assert original.retention_tier == "cold"
    assert datetime.fromisoformat(original.retention_expires_at or "") == (
        ended_at + timedelta(days=21)
    )

    owner.finalize_terminal(
        FinalizeTimelineEvidenceCommand(
            identity=identity,
            outcome="timed_out",
            ended_at=(ended_at + timedelta(days=1)).isoformat(),
        )
    )
    retried = RunManifest.load(run.run_dir)
    assert retried.ended_at == original.ended_at
    assert retried.retention_expires_at == original.retention_expires_at
    assert retried.outcome == "failed"


def test_pin_survives_expiry_until_unpin_then_expires_immediately(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 13, 12, tzinfo=timezone.utc)]
    worktree = tmp_path / "worktree"
    run_dir = _terminal_run(
        worktree, issue_number=42, expires_at=clock[0] + timedelta(days=1)
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=_timeline_store_with_run(
            tmp_path / "timeline.sqlite", issue_number=42, run_dir=run_dir
        ),
        now=lambda: clock[0],
    )
    owner.archive_worktree(42, worktree)
    archived = owner.archive_root / "42" / run_dir.name
    identity = TimelineEvidenceIdentity(42, archived)

    pinned = owner.set_pinned(SetTimelineEvidencePinCommand(identity, pinned=True))
    assert pinned.status is TimelineEvidenceStatus.PINNED

    clock[0] += timedelta(days=2)
    assert owner.prune_expired() == 0
    pinned_after_expiry = owner.describe(identity)
    assert pinned_after_expiry is not None
    assert pinned_after_expiry.unpin_expires_immediately is True
    assert (
        owner.set_pinned(SetTimelineEvidencePinCommand(identity, pinned=True)).status
        is TimelineEvidenceStatus.PINNED
    )

    unpinned = owner.set_pinned(SetTimelineEvidencePinCommand(identity, pinned=False))
    assert unpinned.status is TimelineEvidenceStatus.EXPIRED
    assert (
        owner.set_pinned(SetTimelineEvidencePinCommand(identity, pinned=False)).status
        is TimelineEvidenceStatus.EXPIRED
    )
    assert sorted(path.name for path in archived.iterdir()) == ["manifest.json"]
    assert RunManifest.load(archived).evidence_available is False


def test_cannot_pin_evidence_after_logical_expiry(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    worktree = tmp_path / "worktree"
    run_dir = _terminal_run(
        worktree, issue_number=42, expires_at=now - timedelta(seconds=1)
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=_timeline_store_with_run(
            tmp_path / "timeline.sqlite", issue_number=42, run_dir=run_dir
        ),
        now=lambda: now,
    )
    owner.archive_worktree(42, worktree)
    identity = TimelineEvidenceIdentity(42, owner.archive_root / "42" / run_dir.name)

    with pytest.raises(FileNotFoundError, match="expired"):
        owner.set_pinned(SetTimelineEvidencePinCommand(identity, pinned=True))


def test_pin_rejects_a_run_not_referenced_by_the_issue_timeline(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    run_dir = _terminal_run(
        tmp_path / "worktree",
        issue_number=42,
        expires_at=now + timedelta(days=7),
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=SqliteTimelineStore(tmp_path / "timeline.sqlite"),
        now=lambda: now,
    )

    with pytest.raises(ValueError, match="exact issue/run pair"):
        owner.set_pinned(
            SetTimelineEvidencePinCommand(
                TimelineEvidenceIdentity(42, run_dir),
                pinned=True,
            )
        )
    assert RunManifest.load(run_dir).retention_pinned is False


def test_pin_rejects_an_active_run(tmp_path: Path) -> None:
    output = FileSystemSessionOutput()
    run = output.start_run(tmp_path / "worktree", "issue-42", issue_number=42)
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=_timeline_store_with_run(
            tmp_path / "timeline.sqlite", issue_number=42, run_dir=run.run_dir
        ),
    )

    with pytest.raises(ValueError, match="only after a run is terminal"):
        owner.set_pinned(
            SetTimelineEvidencePinCommand(
                TimelineEvidenceIdentity(42, run.run_dir),
                pinned=True,
            )
        )


def test_archive_rejects_run_owned_by_another_issue(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    worktree = tmp_path / "worktree"
    _terminal_run(worktree, issue_number=7, expires_at=now + timedelta(days=1))
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=SqliteTimelineStore(tmp_path / "timeline.sqlite"),
        now=lambda: now,
    )

    with pytest.raises(ValueError, match="belongs to issue 7"):
        owner.archive_worktree(42, worktree)
    assert not (owner.archive_root / "42").exists()


def test_archive_skips_runs_not_referenced_by_the_issue_timeline(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    worktree = tmp_path / "worktree"
    run_dir = _terminal_run(
        worktree, issue_number=42, expires_at=now + timedelta(days=7)
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=SqliteTimelineStore(tmp_path / "timeline.sqlite"),
        now=lambda: now,
    )

    assert owner.archive_worktree(42, worktree) == 0
    assert not (owner.archive_root / "42" / run_dir.name).exists()


def test_archive_copies_declared_claude_log_link_as_a_regular_file(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    worktree = tmp_path / "worktree"
    run_dir = _terminal_run(
        worktree, issue_number=42, expires_at=now + timedelta(days=7)
    )
    external_log = tmp_path / "claude-logs" / "session.jsonl"
    external_log.parent.mkdir()
    external_log.write_text('{"type":"assistant"}\n')
    (run_dir / "claude-session.jsonl").symlink_to(external_log)
    (run_dir / "claude-session.path").write_text(str(external_log))
    (run_dir / "claude-log.path").write_text(str(external_log.parent))
    manifest = RunManifest.load(run_dir)
    manifest.claude_log_path = str(external_log)
    artifacts = manifest.artifacts or {}
    artifacts["claude_log"] = {
        "kind": "claude_jsonl",
        "path": str(external_log),
    }
    manifest.artifacts = artifacts
    manifest.save()
    FileSystemSessionOutput().update_manifest(
        run_dir, {"claude_log_dir": str(external_log.parent)}
    )
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=_timeline_store_with_run(
            tmp_path / "timeline.sqlite", issue_number=42, run_dir=run_dir
        ),
        now=lambda: now,
    )

    assert owner.archive_worktree(42, worktree) == 1

    archived = owner.archive_root / "42" / run_dir.name
    archived_log = archived / "claude-session.jsonl"
    assert archived_log.is_file()
    assert not archived_log.is_symlink()
    assert archived_log.read_text() == external_log.read_text()
    archived_manifest = RunManifest.load(archived)
    assert archived_manifest.claude_log_path == str(archived_log)
    assert archived_manifest.to_dict().get("claude_log_dir") is None
    assert archived_manifest.artifacts["claude_log"]["path"] == str(archived_log)
    assert (archived / "claude-session.path").read_text() == str(archived_log)
    assert (archived / "claude-log.path").read_text() == str(archived)


def test_archive_rejects_undeclared_symlinks(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    worktree = tmp_path / "worktree"
    run_dir = _terminal_run(
        worktree, issue_number=42, expires_at=now + timedelta(days=7)
    )
    external_file = tmp_path / "unexpected.txt"
    external_file.write_text("not an owned run artifact")
    (run_dir / "unexpected-link").symlink_to(external_file)
    owner = FileSystemTimelineEvidence(
        archive_root=tmp_path / "state" / "timeline-evidence",
        timeline_store=_timeline_store_with_run(
            tmp_path / "timeline.sqlite", issue_number=42, run_dir=run_dir
        ),
        now=lambda: now,
    )

    with pytest.raises(RuntimeError, match="may not contain symlinks"):
        owner.archive_worktree(42, worktree)
    assert not (owner.archive_root / "42" / run_dir.name).exists()


def test_pruning_rejects_a_symlinked_cadence_marker(tmp_path: Path) -> None:
    archive_root = tmp_path / "state" / "timeline-evidence"
    archive_root.mkdir(parents=True)
    external_file = tmp_path / "external-marker"
    external_file.write_text("do not touch")
    marker = archive_root / ".last-pruned"
    marker.symlink_to(external_file)
    owner = FileSystemTimelineEvidence(
        archive_root=archive_root,
        timeline_store=SqliteTimelineStore(tmp_path / "timeline.sqlite"),
    )

    with pytest.raises(RuntimeError, match="Unsafe Timeline evidence prune marker"):
        owner.prune_due()
    with pytest.raises(RuntimeError, match="Unsafe Timeline evidence prune marker"):
        owner.prune_expired()
    assert external_file.read_text() == "do not touch"
