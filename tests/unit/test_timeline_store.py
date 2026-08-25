"""Unit tests for timeline storage and writer adapters."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading

import pytest

from issue_orchestrator.execution.timeline_store import (
    SqliteTimelineStore,
    TimelineStoreConfig,
)
from issue_orchestrator.execution.timeline_artifact_expectations import (
    REVIEW_PHASE_LOG_TIMELINE_EVENTS,
)
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports.event_sink import TraceEvent, run_scoped_event_names
from issue_orchestrator.ports.timeline_store import TimelineRecord, TimelineStore
from issue_orchestrator.events import EventName
from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION
from issue_orchestrator.timeline import TIMELINE_SCHEMA_VERSION, build_issue_timeline


class RecordingTimelineStore(TimelineStore):
    def __init__(self) -> None:
        self.records: list[TimelineRecord] = []

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        self.records.append(record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        return self.records


def test_sqlite_timeline_store_trims_max_records(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=2),
    )
    issue = 42
    store.append(
        issue, TimelineRecord(event_id="1", timestamp="t1", event="e1", data={})
    )
    store.append(
        issue, TimelineRecord(event_id="2", timestamp="t2", event="e2", data={})
    )
    store.append(
        issue, TimelineRecord(event_id="3", timestamp="t3", event="e3", data={})
    )

    records = store.read(issue)
    assert [record.event_id for record in records] == ["2", "3"]


def test_sqlite_timeline_store_read_limit_returns_tail(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    issue = 7
    store.append(
        issue, TimelineRecord(event_id="a", timestamp="t1", event="e1", data={})
    )
    store.append(
        issue, TimelineRecord(event_id="b", timestamp="t2", event="e2", data={})
    )
    store.append(
        issue, TimelineRecord(event_id="c", timestamp="t3", event="e3", data={})
    )

    records = store.read(issue, limit=1)
    assert len(records) == 1
    assert records[0].event_id == "c"


def test_sqlite_timeline_store_reuses_one_connection_across_reader_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from issue_orchestrator.execution import timeline_store as timeline_store_module

    original_open_sqlite = timeline_store_module.open_sqlite
    calls: list[bool | None] = []
    calls_lock = threading.Lock()

    def counting_open_sqlite(*args: object, **kwargs: object):
        with calls_lock:
            check_same_thread = kwargs.get("check_same_thread")
            assert check_same_thread is None or isinstance(check_same_thread, bool)
            calls.append(check_same_thread)
        return original_open_sqlite(*args, **kwargs)

    monkeypatch.setattr(timeline_store_module, "open_sqlite", counting_open_sqlite)

    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(1, TimelineRecord(event_id="one", timestamp="t1", event="e1", data={}))

    barrier = threading.Barrier(8)

    def read_records() -> list[str]:
        barrier.wait(timeout=5)
        return [record.event_id for record in store.read(1)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: read_records(), range(8)))

    assert results == [["one"]] * 8
    assert calls == [False]


def test_read_timeline_records_closes_short_lived_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from issue_orchestrator.execution import timeline_store as timeline_store_module

    expected = [TimelineRecord(event_id="one", timestamp="t1", event="e1", data={})]
    instances = []

    class ClosingStore:
        def __init__(self, db_path: Path) -> None:
            self.db_path = db_path
            self.closed = False
            self.read_args: tuple[int, int | None] | None = None
            instances.append(self)

        def __enter__(self) -> "ClosingStore":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self.closed = True

        def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
            self.read_args = (issue_number, limit)
            return expected

    monkeypatch.setattr(timeline_store_module, "SqliteTimelineStore", ClosingStore)

    records = timeline_store_module.read_timeline_records(
        tmp_path / "timeline.sqlite",
        42,
        limit=5,
    )

    assert records == expected
    assert len(instances) == 1
    assert instances[0].read_args == (42, 5)
    assert instances[0].closed is True


def test_sqlite_timeline_store_trims_total_records_across_issues(
    tmp_path: Path,
) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=100, max_total_records=3),
    )
    store.append(1, TimelineRecord(event_id="a", timestamp="t1", event="e1", data={}))
    store.append(2, TimelineRecord(event_id="b", timestamp="t2", event="e2", data={}))
    store.append(1, TimelineRecord(event_id="c", timestamp="t3", event="e3", data={}))
    store.append(2, TimelineRecord(event_id="d", timestamp="t4", event="e4", data={}))

    assert [record.event_id for record in store.read(1)] == ["c"]
    assert [record.event_id for record in store.read(2)] == ["b", "d"]


def test_sqlite_timeline_store_preserves_run_dir_payload(tmp_path: Path) -> None:
    run_dir = (
        tmp_path
        / "wt-4057"
        / ".issue-orchestrator"
        / "sessions"
        / "20260217-000001Z__issue-4057"
    )
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        4057,
        TimelineRecord(
            event_id="run-event",
            timestamp="2026-02-17T00:00:01Z",
            event="session.started",
            data={"issue_number": 4057, "run_dir": str(run_dir), "task": "code"},
        ),
    )

    records = store.read(4057)
    assert len(records) == 1
    assert records[0].data["run_dir"] == str(run_dir)
    assert records[0].data["task"] == "code"


def test_issue_timeline_preserves_explicit_event_artifacts() -> None:
    payload = build_issue_timeline(
        4057,
        [
            TimelineRecord(
                event_id="validation-1",
                timestamp="2026-02-17T00:00:01Z",
                event="validation.passed",
                source_event="session.validation_passed",
                data={
                    "issue_number": 4057,
                    "artifacts": [
                        {
                            "type": "validation",
                            "label": "Validation Record",
                            "value": "/tmp/run/validation-record.json",
                        }
                    ],
                    "run_dir": "/tmp/run",
                },
            ),
        ],
    )

    assert payload["events"][0]["artifacts"] == [
        {
            "type": "validation",
            "label": "Validation Record",
            "value": "/tmp/run/validation-record.json",
        },
        {"type": "run_dir", "label": "Run Dir", "value": "/tmp/run"},
    ]


@pytest.mark.parametrize(
    ("artifact_kind", "artifact_label", "render_mode"),
    [
        ("prompt", "Prompt", "text"),
        ("review_response", "Review Response", "json"),
        ("chapter_sidecar", "Replay Chapters", "json"),
    ],
)
def test_issue_timeline_projects_typed_artifact_refs_and_role_context(
    artifact_kind: str,
    artifact_label: str,
    render_mode: str,
) -> None:
    artifact_path = (
        f"/tmp/run/review-exchange/turns/"
        f"round-1-reviewer-attempt-2.{artifact_kind}"
    )
    payload = build_issue_timeline(
        4057,
        [
            TimelineRecord(
                event_id="reviewer-prompted-1",
                timestamp="2026-02-17T00:00:01Z",
                event="review_exchange.role_prompted",
                data={
                    "issue_number": 4057,
                    "run_dir": "/tmp/run",
                    "round_index": 1,
                    "attempt_index": 2,
                    "role": "reviewer",
                    "artifact_refs": [
                        {
                            "kind": artifact_kind,
                            "label": artifact_label,
                            "path": artifact_path,
                            "render_mode": render_mode,
                        },
                    ],
                },
            ),
        ],
    )

    event = payload["events"][0]
    assert event["role"] == "reviewer"
    assert event["attempt_index"] == 2
    assert event["artifacts"][0] == {
        "type": artifact_kind,
        "label": artifact_label,
        "value": artifact_path,
        "render_mode": render_mode,
    }


def test_issue_timeline_rejects_duplicate_artifact_identities() -> None:
    duplicate_path = "/tmp/run/review-exchange/turns/round-1-reviewer-attempt-1.prompt.md"

    with pytest.raises(ValueError, match="duplicate artifact identity"):
        build_issue_timeline(
            4057,
            [
                TimelineRecord(
                    event_id="reviewer-prompted-1",
                    timestamp="2026-02-17T00:00:01Z",
                    event="review_exchange.role_prompted",
                    data={
                        "issue_number": 4057,
                        "artifacts": [
                            {
                                "type": "prompt",
                                "label": "Legacy Prompt",
                                "value": duplicate_path,
                            },
                        ],
                        "artifact_refs": [
                            {
                                "kind": "prompt",
                                "label": "Prompt",
                                "path": duplicate_path,
                                "render_mode": "text",
                            },
                        ],
                    },
                ),
            ],
        )


def test_issue_timeline_rejects_malformed_explicit_event_artifacts() -> None:
    with pytest.raises(ValueError, match="artifacts require non-empty"):
        build_issue_timeline(
            4057,
            [
                TimelineRecord(
                    event_id="validation-1",
                    timestamp="2026-02-17T00:00:01Z",
                    event="validation.passed",
                    source_event="session.validation_passed",
                    data={
                        "issue_number": 4057,
                        "artifacts": [
                            {
                                "type": "validation",
                                "label": "Validation Record",
                                "value": "",
                            }
                        ],
                    },
                ),
            ],
        )


@pytest.mark.parametrize(
    "artifact_ref",
    [
        {"kind": "", "label": "Prompt", "path": "/tmp/prompt.md"},
        {"kind": "prompt", "label": "", "path": "/tmp/prompt.md"},
        {"kind": "prompt", "label": "Prompt"},
        {"kind": 123, "label": "Prompt", "path": "/tmp/prompt.md"},
    ],
)
def test_issue_timeline_rejects_malformed_artifact_refs(
    artifact_ref: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="artifact_refs"):
        build_issue_timeline(
            4057,
            [
                TimelineRecord(
                    event_id="reviewer-prompted-1",
                    timestamp="2026-02-17T00:00:01Z",
                    event="review_exchange.role_prompted",
                    data={
                        "issue_number": 4057,
                        "artifact_refs": [artifact_ref],
                    },
                ),
            ],
        )


def test_timeline_writer_rejects_malformed_artifact_refs_before_store_append() -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)

    with pytest.raises(ValueError, match="artifact_refs"):
        writer.record(
            TraceEvent(
                EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
                {
                    "issue_number": 4057,
                    "run_dir": "/tmp/run",
                    "round_index": 1,
                    "attempt_index": 1,
                    "role": "reviewer",
                    "artifact_refs": [
                        {
                            "kind": "prompt",
                            "label": "Prompt",
                            "path": "",
                        },
                    ],
                },
            )
        )

    assert store.records == []


def test_sqlite_timeline_store_ignores_legacy_jsonl_files(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "timeline"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "issue-4057.jsonl"
    legacy_records = [
        {
            "event_id": "legacy-1",
            "timestamp": "2026-02-17T00:00:01Z",
            "event": "session.started",
            "data": {"issue_number": 4057, "run_dir": "/tmp/run-1"},
        },
        {
            "event_id": "legacy-2",
            "timestamp": "2026-02-17T00:10:01Z",
            "event": "session.completed",
            "data": {"issue_number": 4057},
        },
    ]
    legacy_path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in legacy_records)
        + "\n",
        encoding="utf-8",
    )

    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    # Forward-only policy: legacy JSONL is ignored by the SQLite store.
    records = store.read(4057)
    assert records == []


def test_sqlite_timeline_store_fails_fast_if_db_file_is_replaced(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "timeline.sqlite"
    store = SqliteTimelineStore(
        db_path,
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        42,
        TimelineRecord(
            event_id="a",
            timestamp="t1",
            event="session.started",
            data={"issue_number": 42, "run_dir": "/tmp/run-42"},
        ),
    )

    replacement = tmp_path / "replacement.sqlite"
    replacement.write_text("", encoding="utf-8")
    os.replace(replacement, db_path)

    with pytest.raises(RuntimeError, match="replaced on disk"):
        store.read(42)


def test_sqlite_timeline_store_requires_run_dir_for_run_scoped_events(
    tmp_path: Path,
) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )

    with pytest.raises(RuntimeError, match="requires non-empty run_dir"):
        store.append(
            42,
            TimelineRecord(
                event_id="missing-run-dir",
                timestamp="t1",
                event="session.started",
                data={"issue_number": 42},
            ),
        )


def test_sqlite_timeline_store_delete_removes_issue_events(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=100),
    )
    store.append(42, TimelineRecord(event_id="1", timestamp="t1", event="e1", data={}))
    store.append(42, TimelineRecord(event_id="2", timestamp="t2", event="e2", data={}))
    store.append(99, TimelineRecord(event_id="3", timestamp="t3", event="e3", data={}))

    deleted = store.delete(42)
    assert deleted == 2
    assert store.read(42) == []
    assert len(store.read(99)) == 1, "other issue's events are preserved"


def test_sqlite_writer_covers_all_event_names(tmp_path: Path) -> None:
    issue_number = 4057
    sqlite_store = SqliteTimelineStore(
        tmp_path / "sqlite-root" / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10000, max_total_records=20000),
    )
    sqlite_writer = DefaultTimelineWriter(sqlite_store)

    base_time = datetime(2026, 2, 17, 12, 0, 0, tzinfo=timezone.utc)
    for idx, event_name in enumerate(EventName, start=1):
        run_dir = tmp_path / "runs" / f"run-{idx}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "ui-session.log").write_text("log\n", encoding="utf-8")
        completion_path = run_dir / "completion-agent_backend.json"
        completion_path.write_text('{"status":"completed"}\n', encoding="utf-8")
        payload = {
            "issue_number": issue_number,
            "run_id": f"run-{idx}",
            "run_dir": str(run_dir),
            "completion_path_absolute": str(completion_path),
            "agent": "agent:backend",
            "task": "review" if "review" in event_name.value else "code",
            "rework_cycle": idx % 3,
            "reviewer_agent": "agent:reviewer",
            "added": ["label-a", "label-b"],
            "removed": ["label-c"],
            "summary": f"summary-{idx}",
            "metadata": {"event_index": idx, "event_name": event_name.value},
            "path_value": Path(f"/tmp/path-{idx}"),
            "when": base_time + timedelta(seconds=idx),
        }
        event = TraceEvent(
            event_name,
            payload,
            event_id=idx,
            timestamp=base_time + timedelta(minutes=idx),
        )
        sqlite_writer.record(event)

    sqlite_records = sqlite_store.read(issue_number)
    assert len(sqlite_records) == len(EventName)
    for record in sqlite_records:
        assert record.data["schema"] == EVENT_SCHEMA_VERSION
        assert record.data["timeline_schema_version"] == TIMELINE_SCHEMA_VERSION


def test_timeline_writer_normalizes_non_json_types() -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    event = TraceEvent(
        EventName.TICK_STARTED,
        {
            "issue_number": 1,
            "path": Path("/tmp/example"),
            "values": {"a", "b"},
            "when": datetime(2026, 2, 6, 12, 0, 0, tzinfo=timezone.utc),
        },
    )

    writer.record(event)
    assert store.records
    data = store.records[0].data
    assert data["path"] == "/tmp/example"
    assert sorted(data["values"]) == ["a", "b"]
    assert data["when"].startswith("2026-02-06T12:00:00")


def test_timeline_writer_preserves_sequenced_event_id_and_schema(
    tmp_path: Path,
) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "r77__issue-4057"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("", encoding="utf-8")
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {"issue_number": 4057, "task": "code", "run_dir": str(run_dir)},
        event_id=77,
    )

    writer.record(event)
    assert len(store.records) == 1
    record = store.records[0]
    assert record.event_id == "77"
    assert record.data["schema"] == EVENT_SCHEMA_VERSION
    assert record.data["timeline_schema_version"] == TIMELINE_SCHEMA_VERSION
    assert record.data["event_intent"] == "coding"
    assert record.data["logical_run"] == 1
    assert record.data["logical_cycle"] == 1
    assert record.data["logical_phase"] == "coding"


def test_timeline_writer_review_round_after_validation_retry_keeps_cycle(
    tmp_path: Path,
) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    issue_number = 364
    code_run_dir = tmp_path / "sessions" / "r364__issue-364"
    review_run_dir = tmp_path / "sessions" / "r364-review__issue-364"
    code_run_dir.mkdir(parents=True)
    review_run_dir.mkdir(parents=True)
    (code_run_dir / "ui-session.log").write_text("", encoding="utf-8")
    (review_run_dir / "ui-session.log").write_text("", encoding="utf-8")

    def record(event_name: EventName, data: dict[str, object]) -> None:
        writer.record(
            TraceEvent(
                event_name,
                {
                    "issue_number": issue_number,
                    **data,
                },
            )
        )

    record(
        EventName.REVIEW_APPROVED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
            "rounds": 1,
        },
    )
    record(
        EventName.SESSION_VALIDATION_RETRY_NEEDED,
        {"run_dir": str(code_run_dir)},
    )
    record(
        EventName.SESSION_STARTED,
        {
            "run_dir": str(code_run_dir),
            "task": "code",
        },
    )
    record(
        EventName.REVIEW_STARTED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
        },
    )
    record(
        EventName.REVIEW_EXCHANGE_STARTED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
        },
    )
    record(
        EventName.REVIEW_EXCHANGE_ROUND_STARTED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
            "round_index": 1,
        },
    )
    record(
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
            "round_index": 1,
            "reviewer_response_type": "ok",
        },
    )
    record(
        EventName.REVIEW_EXCHANGE_COMPLETED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
            "rounds": 1,
            "status": "ok",
        },
    )
    record(
        EventName.REVIEW_APPROVED,
        {
            "run_dir": str(review_run_dir),
            "task": "review",
            "rounds": 1,
        },
    )

    retry_review_records = [
        record
        for record in store.records
        if record.source_event
        in {
            EventName.REVIEW_STARTED.value,
            EventName.REVIEW_EXCHANGE_STARTED.value,
            EventName.REVIEW_EXCHANGE_ROUND_STARTED.value,
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED.value,
            EventName.REVIEW_EXCHANGE_COMPLETED.value,
            EventName.REVIEW_APPROVED.value,
        }
    ][1:]
    assert retry_review_records
    assert {record.data["logical_cycle"] for record in retry_review_records} == {2}
    assert retry_review_records[-1].event == "review.approved"


def test_timeline_writer_overwrites_stale_schema_versions(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "r88__issue-4057"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("", encoding="utf-8")
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {
            "issue_number": 4057,
            "task": "code",
            "run_dir": str(run_dir),
            "schema": -1,
            "timeline_schema_version": -1,
        },
        event_id=88,
    )

    writer.record(event)
    assert len(store.records) == 1
    record = store.records[0]
    assert record.data["schema"] == EVENT_SCHEMA_VERSION
    assert record.data["timeline_schema_version"] == TIMELINE_SCHEMA_VERSION


def test_timeline_writer_requires_session_artifact_for_session_started(
    tmp_path: Path,
) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "r1__issue-4057"
    run_dir.mkdir(parents=True)
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {"issue_number": 4057, "run_dir": str(run_dir), "task": "code"},
    )
    with pytest.raises(RuntimeError, match="session_artifact_missing"):
        writer.record(event)

    (run_dir / "ui-session.log").write_text("", encoding="utf-8")
    writer.record(event)
    assert len(store.records) == 1


def test_timeline_writer_requires_completion_record_for_session_completed(
    tmp_path: Path,
) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    completion = tmp_path / "completion.json"
    event = TraceEvent(
        EventName.SESSION_COMPLETED,
        {
            "issue_number": 4057,
            "run_dir": str(tmp_path),
            "completion_path_absolute": str(completion),
            "task": "code",
        },
    )
    with pytest.raises(RuntimeError, match="missing_path"):
        writer.record(event)

    completion.write_text('{"status":"completed"}\n', encoding="utf-8")
    writer.record(event)
    assert len(store.records) == 1


def test_timeline_writer_requires_review_feedback_reference_for_review_comment_added() -> (
    None
):
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    event = TraceEvent(
        EventName.REVIEW_COMMENT_ADDED,
        {"issue_number": 4057, "comment_url": "", "task": "review"},
    )
    with pytest.raises(RuntimeError, match="missing_review_feedback_reference"):
        writer.record(event)

    writer.record(
        TraceEvent(
            EventName.REVIEW_COMMENT_ADDED,
            {
                "issue_number": 4057,
                "comment_url": "https://github.com/org/repo/pull/4124#discussion_r1",
                "task": "review",
            },
        )
    )
    assert len(store.records) == 1


# ---------------------------------------------------------------------------
# source_event round-trip through SQLite
# ---------------------------------------------------------------------------


def test_sqlite_store_persists_source_event(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        42,
        TimelineRecord(
            event_id="fan-1",
            timestamp="t1",
            event="agent.coding_started",
            data={"issue_number": 42, "run_dir": "/tmp/run-42"},
            source_event="session.started",
        ),
    )

    records = store.read(42)
    assert len(records) == 1
    assert records[0].event == "agent.coding_started"
    assert records[0].source_event == "session.started"


def test_sqlite_store_source_event_defaults_to_event_name(tmp_path: Path) -> None:
    """When source_event is not set, it defaults to the event name."""
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        42,
        TimelineRecord(event_id="no-source", timestamp="t1", event="e1", data={}),
    )

    records = store.read(42)
    assert records[0].source_event == "e1"


def test_sqlite_store_run_dir_check_uses_source_event(tmp_path: Path) -> None:
    """The CHECK constraint should use source_event for run_dir validation."""
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    # Fan-out event: external name is "agent.coding_started" but source is "session.started"
    # which requires run_dir. Should fail without run_dir.
    with pytest.raises(RuntimeError, match="requires non-empty run_dir"):
        store.append(
            42,
            TimelineRecord(
                event_id="bad",
                timestamp="t1",
                event="agent.coding_started",
                data={"issue_number": 42},
                source_event="session.started",
            ),
        )


def test_writer_fan_out_records_have_views_tags(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("log")
    writer.record(
        TraceEvent(
            EventName.SESSION_STARTED,
            {"issue_number": 42, "run_dir": str(run_dir), "task": "code"},
        )
    )

    assert len(store.records) >= 1
    for record in store.records:
        assert "views" in record.data
        assert isinstance(record.data["views"], list)
        assert record.source_event == "session.started"


def test_writer_fan_out_narrative_stored_in_data(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("log")
    writer.record(
        TraceEvent(
            EventName.SESSION_STARTED,
            {"issue_number": 42, "run_dir": str(run_dir), "task": "code"},
        )
    )

    user_record = next(r for r in store.records if r.event == "agent.coding_started")
    assert user_record.data["narrative"] == "Coding agent started"


# ---------------------------------------------------------------------------
# Narrative enrichment tests
# ---------------------------------------------------------------------------


class TestNarrativeEnrichment:
    """Test that _enrich_narrative injects dynamic data into static narratives."""

    def _write_and_get_narrative(self, event_name: EventName, data: dict) -> str:
        store = RecordingTimelineStore()
        writer = DefaultTimelineWriter(store)
        data.setdefault("issue_number", 42)
        if (
            event_name in run_scoped_event_names()
            or event_name.value in REVIEW_PHASE_LOG_TIMELINE_EVENTS
        ):
            data.setdefault("run_dir", "/tmp/review-phase-run")
        writer.record(TraceEvent(event_name, data))
        for r in store.records:
            if r.data.get("narrative"):
                return r.data["narrative"]
        return ""

    def test_round_started_includes_round_number(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            {"round_index": 2},
        )
        assert narrative == "Review round 2 started"

    def test_round_completed_includes_verdict(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            {
                "round_index": 3,
                "reviewer_response_type": "changes_requested",
                "coder_response_type": "ok",
            },
        )
        assert narrative == "Review round 3 completed — changes_requested"

    def test_round_completed_without_verdict(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            {"round_index": 1},
        )
        assert narrative == "Review round 1 completed"

    def test_review_approved_includes_round_count(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_APPROVED,
            {"rounds": 4},
        )
        assert narrative == "Review approved after 4 rounds"

    def test_review_approved_single_round_uses_default(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_APPROVED,
            {"rounds": 1},
        )
        assert narrative == "Review approved"

    def test_review_approved_cached_replay_overrides_round_count(self) -> None:
        # Regression: when the approval is a cached replay from a prior
        # orchestrator run, the "approved after N rounds" narrative
        # misleads viewers into thinking N rounds happened in this run.
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_APPROVED,
            {"rounds": 2, "cached": True},
        )
        assert narrative == "Cached review approval reused for unchanged commit"
        assert "rounds" not in narrative
        assert "prior run" not in narrative.lower()

    def _make_run_dir_with_recording(self, tmp_path, name: str) -> str:
        run_dir = tmp_path / name
        run_dir.mkdir()
        (run_dir / "terminal-recording.jsonl").write_text("")
        return str(run_dir)

    def test_review_started_cached_replay_uses_replay_narrative(self, tmp_path) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_STARTED,
            {
                "cached": True,
                "run_dir": self._make_run_dir_with_recording(
                    tmp_path, "prior-review-run"
                ),
            },
        )
        assert narrative == "Cached review result reused for unchanged commit"

    def test_review_started_fresh_uses_default_narrative(self, tmp_path) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_STARTED,
            {
                "run_dir": self._make_run_dir_with_recording(
                    tmp_path, "current-review-run"
                )
            },
        )
        assert narrative == "Code review started"

    def test_issue_unblocked_from_scratch_uses_scratch_narrative(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.ISSUE_UNBLOCKED,
            {"from_scratch": True},
        )
        assert narrative == "Scratch reset requested"

    def test_session_started_from_scratch_uses_scratch_narrative(self, tmp_path) -> None:
        run_dir = tmp_path / "scratch-run"
        run_dir.mkdir()
        (run_dir / "terminal-recording.jsonl").write_text("")
        narrative = self._write_and_get_narrative(
            EventName.SESSION_STARTED,
            {"reset_from_scratch": True, "run_dir": str(run_dir)},
        )
        assert narrative == "Scratch coding agent started"

    def test_changes_requested_cached_replay_uses_replay_narrative(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_CHANGES_REQUESTED,
            {"rounds": 3, "cached": True},
        )
        assert narrative == "Cached changes-requested verdict reused for unchanged commit"
        assert "round 3" not in narrative.lower()

    def test_pr_created_includes_pr_number(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.ISSUE_PR_CREATED,
            {"pr_number": 4623},
        )
        assert narrative == "PR #4623 created"

    def test_exchange_completed_includes_round_count(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_EXCHANGE_COMPLETED,
            {"rounds": 3},
        )
        assert narrative == "Review exchange completed (3 rounds)"

    def test_changes_requested_includes_round(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_CHANGES_REQUESTED,
            {"rounds": 2},
        )
        assert narrative == "Reviewer requested changes (round 2)"

    def test_review_rework_started_includes_round_number(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_REWORK_STARTED,
            {"round_index": 2},
        )
        assert narrative == "Coder started rework for review round 2"

    def test_review_rework_completed_includes_round_number(self) -> None:
        narrative = self._write_and_get_narrative(
            EventName.REVIEW_REWORK_COMPLETED,
            {"round_index": 2},
        )
        assert narrative == "Coder completed rework for review round 2"

    def test_unhandled_event_keeps_static_narrative(self) -> None:
        from issue_orchestrator.events.fan_out_pipeline import enrich_narrative

        result = enrich_narrative(
            "Code review started", "review.started", {"issue_number": 42}
        )
        assert result == "Code review started"


def test_cached_replay_across_logical_runs_narrates_reuse_not_rounds(
    tmp_path: Path,
) -> None:
    # Regression for issue #228: when publish/validation failed after a real
    # review exchange, the orchestrator restarted coding from scratch; the
    # replayed review.approved carried the prior run's ``rounds`` field with
    # ``run_dir`` pointing at the original (now-stale) review-exchange
    # session. The writer's default enricher then produced "Review approved
    # after 2 rounds" for a run in which zero rounds actually happened.
    #
    # This test drives the real writer with the event shape that caused the
    # bug and asserts the fresh vs cached approvals tell different stories.
    run_dir = tmp_path / "20260417-053813Z__review-exchange-228"
    run_dir.mkdir()
    (run_dir / "terminal-recording.jsonl").write_text("")
    run_dir_str = str(run_dir)

    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)

    issue_number = 228
    shared_base: dict[str, object] = {
        "issue_number": issue_number,
        "run_dir": run_dir_str,
        "agent": "agent:reviewer",
        "task": "review",
        "review_exchange_mode": "via-local-loop",
    }

    # --- Fresh review exchange (first orchestrator run) ---
    writer.record(TraceEvent(EventName.REVIEW_STARTED, dict(shared_base)))
    writer.record(
        TraceEvent(
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            {**shared_base, "round_index": 1},
        )
    )
    writer.record(
        TraceEvent(
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            {
                **shared_base,
                "round_index": 1,
                "reviewer_response_type": "changes_requested",
            },
        )
    )
    writer.record(
        TraceEvent(
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            {**shared_base, "round_index": 2},
        )
    )
    writer.record(
        TraceEvent(
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            {**shared_base, "round_index": 2, "reviewer_response_type": "ok"},
        )
    )
    writer.record(
        TraceEvent(EventName.REVIEW_EXCHANGE_COMPLETED, {**shared_base, "rounds": 2})
    )
    writer.record(
        TraceEvent(
            EventName.REVIEW_APPROVED,
            {**shared_base, "rounds": 2, "summary": "Looks good."},
        )
    )

    # --- Cache replay after publish/validation failure restarted coding ---
    writer.record(TraceEvent(EventName.REVIEW_STARTED, {**shared_base, "cached": True}))
    writer.record(
        TraceEvent(
            EventName.REVIEW_APPROVED,
            {**shared_base, "rounds": 2, "cached": True, "summary": "Looks good."},
        )
    )

    approved_records = [r for r in store.records if r.event == "review.approved"]
    assert len(approved_records) == 2
    fresh_narrative = approved_records[0].data["narrative"]
    cached_narrative = approved_records[1].data["narrative"]
    assert fresh_narrative == "Review approved after 2 rounds"
    assert cached_narrative == "Cached review approval reused for unchanged commit"
    assert "after 2 rounds" not in cached_narrative
    assert "prior run" not in cached_narrative.lower()

    review_started_records = [r for r in store.records if r.event == "review.started"]
    assert len(review_started_records) == 2
    assert review_started_records[0].data.get("narrative") == "Code review started"
    assert review_started_records[1].data.get("narrative") == "Cached review result reused for unchanged commit"

    # The cached flag must survive from emission into the stored record so
    # downstream consumers (UI, SSE) can key off it.
    assert approved_records[1].data.get("cached") is True
    assert review_started_records[1].data.get("cached") is True


def test_timeline_event_preserves_review_exchange_round_fields() -> None:
    from issue_orchestrator.timeline import TimelineStream

    record = TimelineRecord(
        event_id="round-1",
        timestamp="2026-03-13T15:45:08.617893+00:00",
        event="review_exchange.round_completed",
        source_event="review_exchange.round_completed",
        data={
            "issue_number": 4057,
            "run_dir": "/tmp/run-4057",
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "logical_run": 1,
            "logical_cycle": 1,
            "logical_phase": "review",
            "review_oriented": True,
            "event_intent": "review",
            "views": ["user", "ops", "debug"],
            "round_index": 1,
            "reviewer_response_type": "changes_requested",
            "reviewer_response_text": "Three issues to fix before approval.",
            "coder_response_type": "ok",
            "coder_response_text": "Applied fixes and updated tests.",
        },
    )

    payload = TimelineStream.from_records(4057, [record]).to_dict()
    event = payload["events"][0]
    assert event["round_index"] == 1
    assert event["reviewer_response_type"] == "changes_requested"
    assert event["reviewer_response_text"] == "Three issues to fix before approval."
    assert event["coder_response_type"] == "ok"
    assert event["coder_response_text"] == "Applied fixes and updated tests."
    assert "Round 1" in event["detail"]
    assert "Three issues to fix before approval." in event["detail"]
    assert "Coder response: ok" in event["detail"]


def test_timeline_event_preserves_review_outcome_round_count() -> None:
    from issue_orchestrator.timeline import TimelineStream

    record = TimelineRecord(
        event_id="approved-2",
        timestamp="2026-03-22T13:50:04.655598+00:00",
        event="review.approved",
        source_event="review.approved",
        data={
            "issue_number": 4057,
            "run_dir": "/tmp/review-run-4057",
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "logical_run": 2,
            "logical_cycle": 2,
            "logical_phase": "review",
            "review_oriented": True,
            "event_intent": "review",
            "views": ["user", "ops", "debug"],
            "rounds": 2,
            "narrative": "Review approved after 2 rounds",
            "summary": "Looks good now.",
        },
    )

    payload = TimelineStream.from_records(4057, [record]).to_dict()
    event = payload["events"][0]
    assert event["rounds"] == 2
    assert event["narrative"] == "Review approved after 2 rounds"


class TestTrivialSummarySuppression:
    """Test that trivial summary values like 'completed' are suppressed in narratives."""

    def test_completed_suppressed(self) -> None:
        from issue_orchestrator.view_models.issue_detail import _is_trivial_summary

        assert _is_trivial_summary("completed")
        assert _is_trivial_summary("Completed")

    def test_ok_suppressed(self) -> None:
        from issue_orchestrator.view_models.issue_detail import _is_trivial_summary

        assert _is_trivial_summary("ok")
        assert _is_trivial_summary("started")
        assert _is_trivial_summary("passed")
        assert _is_trivial_summary("failed")

    def test_meaningful_summary_not_suppressed(self) -> None:
        from issue_orchestrator.view_models.issue_detail import _is_trivial_summary

        assert not _is_trivial_summary("Merge conflict in src/foo.py")
        assert not _is_trivial_summary("reviewer_ok")
        assert not _is_trivial_summary("Validation passed. Implementation is correct")

    def test_narrative_rendering_suppresses_completed(self) -> None:
        from issue_orchestrator.view_models.issue_detail import _event_to_narrative

        event = {
            "event": "agent.coding_completed",
            "narrative": "Agent finished coding",
            "summary": "completed",
        }
        result = _event_to_narrative(event)
        assert result == "Agent finished coding"
        assert ": completed" not in result

    def test_narrative_rendering_keeps_meaningful_summary(self) -> None:
        from issue_orchestrator.view_models.issue_detail import _event_to_narrative

        event = {
            "event": "review.approved",
            "narrative": "Review approved after 4 rounds",
            "summary": "Code looks good, all tests pass",
            "agent": "agent:reviewer",
        }
        result = _event_to_narrative(event)
        assert "Code looks good" in result
        assert "(reviewer)" in result


def test_sqlite_timeline_store_instance_id_persisted(tmp_path: Path) -> None:
    """Events carry the instance_id that was set at store construction."""
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        instance_id="test-instance-abc",
    )
    store.append(42, TimelineRecord(event_id="e1", timestamp="t1", event="ev", data={}))

    # Read back via raw SQL to verify the column value
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT instance_id FROM timeline_events WHERE event_id = 'e1'"
    ).fetchone()
    conn.close()
    assert row["instance_id"] == "test-instance-abc"


def test_sqlite_timeline_store_read_returns_instance_id(tmp_path: Path) -> None:
    """read() should populate instance_id on returned TimelineRecords."""
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        instance_id="test-instance-xyz",
    )
    store.append(42, TimelineRecord(event_id="e1", timestamp="t1", event="ev", data={}))

    records = store.read(42)
    assert len(records) == 1
    assert records[0].instance_id == "test-instance-xyz"


def test_sqlite_timeline_store_instance_id_defaults_to_empty(tmp_path: Path) -> None:
    """Without instance_id, the column defaults to empty string."""
    store = SqliteTimelineStore(tmp_path / "timeline.sqlite")
    store.append(42, TimelineRecord(event_id="e1", timestamp="t1", event="ev", data={}))

    import sqlite3

    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT instance_id FROM timeline_events WHERE event_id = 'e1'"
    ).fetchone()
    conn.close()
    assert row["instance_id"] == ""


def test_sqlite_timeline_store_instance_id_filters_correctly(tmp_path: Path) -> None:
    """Different instance_ids write to the same DB but can be queried separately."""
    db_path = tmp_path / "timeline.sqlite"
    store_a = SqliteTimelineStore(db_path, instance_id="instance-a")
    store_b = SqliteTimelineStore(db_path, instance_id="instance-b")

    store_a.append(
        1,
        TimelineRecord(
            event_id="a1", timestamp="2026-01-01T00:00:01Z", event="ev", data={}
        ),
    )
    store_b.append(
        1,
        TimelineRecord(
            event_id="b1", timestamp="2026-01-01T00:00:02Z", event="ev", data={}
        ),
    )
    store_a.append(
        1,
        TimelineRecord(
            event_id="a2", timestamp="2026-01-01T00:00:03Z", event="ev", data={}
        ),
    )

    # Query for instance-a only
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_id FROM timeline_events WHERE instance_id = ? ORDER BY sequence",
        ("instance-a",),
    ).fetchall()
    conn.close()
    assert [r["event_id"] for r in rows] == ["a1", "a2"]
