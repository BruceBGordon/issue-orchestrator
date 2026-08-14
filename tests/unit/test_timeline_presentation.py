"""Timeline presentation shaping tests."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.domain.timeline_evidence import (
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
)
from issue_orchestrator.entrypoints.timeline_presentation import (
    _decorate_timeline_events,
)
from issue_orchestrator.timeline import TIMELINE_SCHEMA_VERSION
from issue_orchestrator.view_models.timeline_evidence_presentation import (
    TimelineEventBatch,
    attach_timeline_evidence,
)


class _EvidenceReader:
    def __init__(self, state: TimelineEvidenceState) -> None:
        self.state = state
        self.calls: list[TimelineEvidenceIdentity] = []

    def describe(self, identity: TimelineEvidenceIdentity) -> TimelineEvidenceState:
        self.calls.append(identity)
        return self.state


class _FailingEvidenceReader:
    def describe(self, identity: TimelineEvidenceIdentity) -> TimelineEvidenceState:
        raise OSError(f"cannot inspect {identity.run_dir}")


def test_decorated_timeline_events_carry_typed_timestamp_detail_value_kinds() -> None:
    event = {
        "event": "e2e.test_completed",
        "timeline_schema_version": 4,
        "timestamp": "2026-05-12T10:00:00Z",
        "finished_at": "2026-05-12T10:05:00Z",
        "started_at": "2026-05-12T10:00:00",
        "summary": "test finished",
    }

    decorated = _decorate_timeline_events([event], issue_number=4057)

    assert decorated[0]["detail_value_kinds"] == {
        "timestamp": "timestamp",
        "finished_at": "timestamp",
    }
    assert "started_at" not in decorated[0]["detail_value_kinds"]


def test_expired_evidence_is_visible_without_broken_file_actions(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "missing-run"
    state = TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(4057, run_dir),
        status=TimelineEvidenceStatus.EXPIRED,
        label="Evidence expired",
        available=False,
        pinned=False,
        archived=True,
        expires_at="2026-08-01T00:00:00+00:00",
        help_text="Retention elapsed.",
    )
    event = {
        "event": "session.failed",
        "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
        "run_dir": str(run_dir),
    }

    decorated = attach_timeline_evidence(
        TimelineEventBatch(_decorate_timeline_events([event], 4057)),
        4057,
        _EvidenceReader(state),
    ).events

    assert decorated[0]["evidence"]["status"] == "expired"
    assert decorated[0]["actions"] == []


def test_pinned_evidence_exposes_exact_run_unpin_action(tmp_path: Path) -> None:
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )

    run = FileSystemSessionOutput().start_run(
        tmp_path / "worktree", "issue-4057", issue_number=4057
    )
    state = TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(4057, run.run_dir),
        status=TimelineEvidenceStatus.PINNED,
        label="Pinned",
        available=True,
        pinned=True,
        archived=False,
        help_text="Retained until unpinned.",
        unpin_expires_immediately=True,
    )
    event = {
        "event": "session.started",
        "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
        "issue_number": 4057,
        "run_dir": str(run.run_dir),
    }

    decorated = attach_timeline_evidence(
        TimelineEventBatch(_decorate_timeline_events([event], 4057)),
        4057,
        _EvidenceReader(state),
    ).events
    action = next(
        item
        for item in decorated[0]["actions"]
        if item["type"] == "set_timeline_evidence_pin"
    )
    assert action["run_dir"] == str(run.run_dir)
    assert action["pinned"] is False
    assert "remove" in action["confirm_message"]


def test_active_run_shows_state_without_premature_pin_action(tmp_path: Path) -> None:
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )

    run = FileSystemSessionOutput().start_run(
        tmp_path / "worktree", "issue-4057", issue_number=4057
    )
    state = TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(4057, run.run_dir),
        status=TimelineEvidenceStatus.ACTIVE,
        label="Active run",
        available=True,
        pinned=False,
        archived=False,
        help_text="Retention starts when this run ends.",
    )
    event = {
        "event": "session.started",
        "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
        "issue_number": 4057,
        "run_dir": str(run.run_dir),
    }

    decorated = attach_timeline_evidence(
        TimelineEventBatch(_decorate_timeline_events([event], 4057)),
        4057,
        _EvidenceReader(state),
    ).events

    assert decorated[0]["evidence"]["status"] == "active"
    assert all(
        action["type"] != "set_timeline_evidence_pin"
        for action in decorated[0]["actions"]
    )


def test_evidence_read_failure_is_visible_and_suppresses_broken_actions(
    tmp_path: Path,
) -> None:
    event = {
        "event": "session.failed",
        "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
        "issue_number": 4057,
        "run_dir": str(tmp_path / "unreadable-run"),
    }

    decorated = attach_timeline_evidence(
        TimelineEventBatch(_decorate_timeline_events([event], 4057)),
        4057,
        _FailingEvidenceReader(),
    ).events

    assert decorated[0]["evidence"]["status"] == "missing"
    assert decorated[0]["evidence"]["label"] == "Evidence unavailable"
    assert decorated[0]["actions"] == []


def test_evidence_is_resolved_once_and_shown_on_the_latest_exact_run_row(
    tmp_path: Path,
) -> None:
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )

    run_dir = FileSystemSessionOutput().start_run(
        tmp_path / "worktree", "issue-4057", issue_number=4057
    ).run_dir
    state = TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(4057, run_dir),
        status=TimelineEvidenceStatus.RETAINED,
        label="Evidence retained",
        available=True,
        pinned=False,
        archived=True,
        help_text="Retained for seven days.",
    )
    reader = _EvidenceReader(state)
    events = [
        {
            "event": "session.started",
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "run_dir": str(run_dir),
        },
        {
            "event": "session.failed",
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "run_dir": str(run_dir),
        },
    ]

    decorated = attach_timeline_evidence(
        TimelineEventBatch(_decorate_timeline_events(events, 4057)),
        4057,
        reader,
    ).events

    assert len(reader.calls) == 1
    assert "evidence" not in decorated[0]
    assert all(
        action["type"] != "set_timeline_evidence_pin"
        for action in decorated[0]["actions"]
    )
    assert decorated[1]["evidence"]["status"] == "retained"
    assert any(
        action["type"] == "set_timeline_evidence_pin"
        for action in decorated[1]["actions"]
    )


def test_archived_success_without_local_tail_hides_orchestrator_log_action(
    tmp_path: Path,
) -> None:
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )

    run = FileSystemSessionOutput().start_run(
        tmp_path / "worktree", "issue-4057", issue_number=4057
    )
    state = TimelineEvidenceState(
        identity=TimelineEvidenceIdentity(4057, run.run_dir),
        status=TimelineEvidenceStatus.RETAINED,
        label="Evidence retained",
        available=True,
        pinned=False,
        archived=True,
        help_text="Retained for seven days.",
    )
    event = {
        "event": "session.completed",
        "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
        "issue_number": 4057,
        "run_dir": str(run.run_dir),
    }

    [decorated] = attach_timeline_evidence(
        TimelineEventBatch(_decorate_timeline_events([event], 4057)),
        4057,
        _EvidenceReader(state),
    ).events

    action_types = {action["type"] for action in decorated["actions"]}
    assert "open_orchestrator_log" not in action_types
    assert "set_timeline_evidence_pin" in action_types
