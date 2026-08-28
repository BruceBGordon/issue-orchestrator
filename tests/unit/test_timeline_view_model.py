"""Unit tests for timeline view model."""

from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.view_models.timeline import TimelineStream, build_issue_timeline


def test_build_issue_timeline_maps_phase_and_step():
    records = [
        TimelineRecord(
            event_id="e1",
            timestamp="2026-02-06T00:00:00Z",
            event="session.started",
            data={"issue_number": 123, "session_id": "issue-123"},
        ),
        TimelineRecord(
            event_id="e2",
            timestamp="2026-02-06T00:01:00Z",
            event="issue.pr_created",
            data={"issue_number": 123, "pr_url": "https://example/pr/1"},
        ),
        TimelineRecord(
            event_id="e3",
            timestamp="2026-02-06T00:02:00Z",
            event="session.completed",
            data={
                "issue_number": 123,
                "completion_path_absolute": "/tmp/worktree/.issue-orchestrator/completion.json",
            },
        ),
        TimelineRecord(
            event_id="e4",
            timestamp="2026-02-06T00:03:00Z",
            event="review.comment_added",
            data={
                "issue_number": 123,
                "pr_number": 777,
                "comment_url": "https://example/pr/777#issuecomment-1",
            },
        ),
    ]

    timeline = build_issue_timeline(123, records)
    events = timeline["events"]

    assert events[0]["phase"] == "in_progress"
    assert events[0]["step"] == "started"
    assert events[1]["phase"] == "orchestrator"
    assert events[1]["step"] == "pr_created"
    assert events[1]["artifacts"][0]["type"] == "pull_request"
    assert events[2]["artifacts"][0]["type"] == "completion_record"
    assert events[3]["phase"] == "reviewing"
    assert events[3]["artifacts"][0]["type"] == "review_comment"

    stream = TimelineStream.from_records(123, records)
    grouped = stream.group_by_phase()
    assert grouped["in_progress"][0].step == "started"


def test_session_start_expected_completion_path_is_not_an_available_artifact():
    event = build_issue_timeline(
        158,
        [
            TimelineRecord(
                event_id="start-158",
                timestamp="2026-08-28T12:00:00Z",
                event="agent.coding_started",
                source_event="session.started",
                data={
                    "issue_number": 158,
                    "completion_path_absolute": "/tmp/run/completion-record.json",
                    "run_dir": "/tmp/run",
                },
            )
        ],
    )["events"][0]

    assert all(
        artifact["type"] != "completion_record" for artifact in event["artifacts"]
    )


def test_build_issue_timeline_maps_validation_and_review_queue_to_orchestrator_phase():
    records = [
        TimelineRecord(
            event_id="v1",
            timestamp="2026-02-06T00:00:00Z",
            event="validation.completed",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="v2",
            timestamp="2026-02-06T00:01:00Z",
            event="review.queued",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="v3",
            timestamp="2026-02-06T00:02:00Z",
            event="session.validation_retry_needed",
            data={"issue_number": 123},
        ),
    ]

    timeline = build_issue_timeline(123, records)
    assert [event["phase"] for event in timeline["events"]] == [
        "orchestrator",
        "orchestrator",
        "orchestrator",
    ]


def test_build_issue_timeline_surfaces_publish_failure_reason():
    records = [
        TimelineRecord(
            event_id="p1",
            timestamp="2026-02-06T00:00:00Z",
            event="publish.failed",
            source_event="publish.failed",
            data={
                "issue_number": 123,
                "stage": "push_branch",
                "branch": "123-feature",
                "retryable": True,
                "error": (
                    "ERROR: Test-skipping patterns detected\n"
                    "+import org.junit.jupiter.api.Assumptions.assumeTrue\n"
                    "error: failed to push some refs"
                ),
            },
        ),
    ]

    event = build_issue_timeline(123, records)["events"][0]

    assert event["phase"] == "orchestrator"
    assert event["status"] == "failed"
    assert event["summary"].startswith("Push failed: ")
    assert "Test-skipping patterns detected" in event["summary"]
    assert "assumeTrue" in event["summary"]
    assert event["detail"] == "Branch: 123-feature. Retryable: yes"


def test_build_issue_timeline_labels_create_pr_publish_failure():
    records = [
        TimelineRecord(
            event_id="p1",
            timestamp="2026-02-06T00:00:00Z",
            event="publish.failed",
            source_event="publish.failed",
            data={
                "issue_number": 123,
                "stage": "create_pr",
                "error": "pull request already exists for branch 123-feature",
            },
        ),
    ]

    event = build_issue_timeline(123, records)["events"][0]

    assert event["summary"].startswith("PR creation failed: ")
    assert "pull request already exists" in event["summary"]


def test_build_issue_timeline_truncates_long_publish_failure_summary():
    long_error = "\n".join(f"hook diagnostic line {idx}" for idx in range(30))
    records = [
        TimelineRecord(
            event_id="p1",
            timestamp="2026-02-06T00:00:00Z",
            event="publish.failed",
            source_event="publish.failed",
            data={
                "issue_number": 123,
                "stage": "push_branch",
                "error": long_error,
            },
        ),
    ]

    event = build_issue_timeline(123, records)["events"][0]

    assert event["summary"].startswith("Push failed: ")
    assert len(event["summary"]) <= 200
    assert event["summary"].endswith("…")


def test_build_issue_timeline_status_mapping():
    records = [
        TimelineRecord(
            event_id="e1",
            timestamp="2026-02-06T00:00:00Z",
            event="review.queued",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e2",
            timestamp="2026-02-06T00:01:00Z",
            event="review.escalated",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e3",
            timestamp="2026-02-06T00:02:00Z",
            event="issue.pr_rejected",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e4",
            timestamp="2026-02-06T00:03:00Z",
            event="session.validation_failed",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e5",
            timestamp="2026-02-06T00:04:00Z",
            event="review.skipped",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e6",
            timestamp="2026-02-06T00:05:00Z",
            event="rework.escalating",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e7",
            timestamp="2026-02-06T00:06:00Z",
            event="review.rework_completed",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e8",
            timestamp="2026-02-06T00:07:00Z",
            event="tech_lead.issue_created",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e9",
            timestamp="2026-02-06T00:08:00Z",
            event="tech_lead.skipped",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e10",
            timestamp="2026-02-06T00:09:00Z",
            event="review.closed",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e11",
            timestamp="2026-02-06T00:10:00Z",
            event="issue.unblocked",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e12",
            timestamp="2026-02-06T00:11:00Z",
            event="provider.outage_entered",
            data={"issue_number": 123},
        ),
    ]

    timeline = build_issue_timeline(123, records)
    statuses = [event["status"] for event in timeline["events"]]

    assert statuses == [
        "started",
        "failed",
        "failed",
        "failed",
        "completed",
        "failed",
        "completed",
        "started",
        "completed",
        "failed",
        "completed",
        "started",
    ]
