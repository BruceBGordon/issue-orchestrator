"""Semantics-first tests for issue detail story synthesis."""

from __future__ import annotations

from types import SimpleNamespace

from issue_orchestrator.domain.models import SessionHistoryEntry
from issue_orchestrator.entrypoints.web_issue_detail_routes import _determine_issue_flow_stage
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TIMELINE_SCHEMA_VERSION, TimelineStream
from issue_orchestrator.view_models.issue_detail import (
    IssueStoryContext,
    _build_journey_steps,
    _build_status_explanation,
    _filter_events_by_view,
    build_issue_detail_view_model,
    filter_last_run_cycles,
)
from issue_orchestrator.view_models.journey_projection import (
    build_journey_cycles_from_events,
    collect_cycle_artifacts,
)
from issue_orchestrator.view_models.lifecycle_semantics import IssueProjectionContext


def _projection_ctx(**overrides: object) -> IssueProjectionContext:
    base: dict[str, object] = {"flow_stage": "queued"}
    base.update(overrides)
    return IssueProjectionContext(**base)  # type: ignore[arg-type]


def _journey_cycles(events: list[dict[str, object]], today: str) -> tuple:
    """Test helper: build typed journey cycles with a default projection context.

    Uses the typed entry point in ``lifecycle_projection``.  Returns a
    tuple of ``IssueCycle`` models with journey fields populated.
    """
    return build_journey_cycles_from_events(
        events,
        today,
        _projection_ctx(),
        issue_number=4124,
    )


def _ctx(**overrides: object) -> IssueStoryContext:
    base: dict[str, object] = {
        "flow_stage": "queued",
        "active_runtime_minutes": None,
        "active_task_kind": None,
        "labels": (),
        "dependency_summary": None,
        "current_rework_cycle": 0,
        "max_rework_cycles": 3,
        "pr_url": None,
        "pr_number": None,
    }
    base.update(overrides)
    return IssueStoryContext(**base)  # type: ignore[arg-type]


def _intent_for(event: str, task: str | None = None) -> str:
    if task == "review" or event.startswith("review.") or event.startswith("review_exchange."):
        return "review"
    if event.startswith("rework.") or task == "rework":
        return "rework"
    if event.startswith("session.") and not event.startswith("session.validation"):
        return "coding"
    return "orchestrator"


def _phase_for(intent: str, logical_cycle: int) -> str:
    if intent == "review":
        return "review"
    if intent == "rework":
        return "rework"
    if intent == "orchestrator":
        return "orchestrator"
    return "rework" if logical_cycle > 1 else "coding"


def _evt(
    event: str,
    *,
    timestamp: str,
    logical_run: int,
    logical_cycle: int,
    task: str | None = None,
    status: str = "started",
    summary: str | None = None,
    agent: str | None = None,
    artifacts: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    intent = _intent_for(event, task)
    return {
        "event": event,
        "timestamp": timestamp,
        "status": status,
        "step": event.split(".")[-1],
        "phase": "in_progress",
        "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
        "event_intent": intent,
        "review_oriented": intent == "review",
        "logical_run": logical_run,
        "logical_cycle": logical_cycle,
        "logical_phase": _phase_for(intent, logical_cycle),
        "task": task,
        "summary": summary,
        "agent": agent,
        "artifacts": artifacts or [],
    }


class _RecordingTimelineStore:
    def __init__(self) -> None:
        self.records: list[TimelineRecord] = []

    def append(self, _issue_number: int, record: TimelineRecord) -> None:
        self.records.append(record)

    def read(
        self,
        _issue_number: int,
        limit: int | None = None,
    ) -> list[TimelineRecord]:
        if limit is None:
            return list(self.records)
        return self.records[-limit:]

    def delete(self, _issue_number: int) -> int:
        deleted = len(self.records)
        self.records.clear()
        return deleted


def _project_user_events_from_trace_events(
    issue_number: int,
    trace_events: list[TraceEvent],
) -> list[dict[str, object]]:
    store = _RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    for event in trace_events:
        writer.record(event)
    stream = TimelineStream.from_records(issue_number, store.records)
    events = stream.to_dict()["events"]
    return _filter_events_by_view(events, "user")


def test_status_explanation_running_review() -> None:
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=7, active_task_kind="review")
    assert _build_status_explanation(ctx, []) == "Code review in progress (7 min)"


def test_status_explanation_queued_for_rework_names_pr_cycle_reason() -> None:
    # Regression (#6588): a rework-queued issue explains itself with the PR
    # number, cycle, and reason before the rework session launches.
    ctx = _ctx(
        flow_stage="queued_for_rework",
        pr_number=469,
        current_rework_cycle=1,
        rework_reason="Merge conflict against base branch",
    )
    assert _build_status_explanation(ctx, []) == (
        "Queued for rework — PR #469 (cycle 1): Merge conflict against base branch"
    )


def test_status_explanation_queued_for_rework_without_pr_falls_back() -> None:
    ctx = _ctx(
        flow_stage="queued_for_rework",
        pr_number=None,
        current_rework_cycle=2,
        rework_reason=None,
    )
    assert _build_status_explanation(ctx, []) == (
        "Queued for rework (cycle 2): Rework requested"
    )


def test_status_explanation_prefers_active_review_exchange_substate() -> None:
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=31, active_task_kind="code")
    events = [
        {
            "event": "review_exchange.role_prompted",
            "source_event": "review_exchange.role_prompted",
            "role": "coder",
            "round_index": 1,
            "narrative": "Coder addressing review nits (round 1)",
        }
    ]

    assert _build_status_explanation(ctx, events) == (
        "Review exchange: coder addressing review nits (round 1) (31 min)"
    )


def test_status_explanation_names_coder_needs_requested_changes_from_response_type() -> None:
    # The reviewer's ``changes_requested`` verdict rides on the role_feedback
    # event's ``response_type``. Before issue #6428 that field was dropped by
    # the timeline projection, leaving this substate branch effectively dead.
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=14, active_task_kind="code")
    events = [
        {
            "event": "review_exchange.role_feedback",
            "source_event": "review_exchange.role_feedback",
            "role": "reviewer",
            "round_index": 1,
            "response_type": "changes_requested",
        }
    ]

    assert _build_status_explanation(ctx, events) == (
        "Review exchange: coder needs requested changes (round 1) (14 min)"
    )


def test_status_explanation_names_review_exchange_exit_before_response() -> None:
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=26, active_task_kind="code")
    events = [
        {
            "event": "review_exchange.role_timeout",
            "source_event": "review_exchange.role_timeout",
            "role": "reviewer",
            "round_index": 2,
            "narrative": "Reviewer exited before responding (round 2)",
        }
    ]

    assert _build_status_explanation(ctx, events) == (
        "Review exchange: reviewer exited before responding (round 2) (26 min)"
    )


def test_status_explanation_surfaces_validation_retry_during_review_exchange() -> None:
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=19, active_task_kind="code")
    events = [
        {
            "event": "review_exchange.role_prompted",
            "source_event": "review_exchange.role_prompted",
            "role": "coder",
            "round_index": 1,
            "narrative": "Coder addressing requested changes (round 1)",
        },
        {
            "event": "session.validation_retry_needed",
            "source_event": "session.validation_retry_needed",
            "narrative": "Validation failed - retrying",
        },
    ]

    assert _build_status_explanation(ctx, events) == (
        "Review exchange: validation failed - retrying (19 min)"
    )


def test_status_explanation_skips_unhandled_review_exchange_debug_event() -> None:
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=12, active_task_kind="code")
    events = [
        {
            "event": "review_exchange.role_prompted",
            "source_event": "review_exchange.role_prompted",
            "role": "reviewer",
            "round_index": 1,
            "narrative": "Reviewer prompt sent (round 1)",
        },
        {
            "event": "review_exchange.chapter_recorded",
            "source_event": "review_exchange.chapter_recorded",
            "role": "reviewer",
            "round_index": 1,
            "narrative": "",
        },
    ]

    assert _build_status_explanation(ctx, events) == (
        "Review exchange: reviewer prompt sent (round 1) (12 min)"
    )


def test_status_explanation_blocked_publish_failed() -> None:
    ctx = _ctx(flow_stage="blocked", labels=("publish-failed",))
    result = _build_status_explanation(ctx, [])
    assert "Publishing failed" in result


def test_status_explanation_blocked_publish_failed_no_matching_event() -> None:
    """publish-failed label without a blocking event uses the label-based explanation."""
    ctx = _ctx(flow_stage="blocked", labels=("publish-failed",))
    # Non-blocking event present — label check still fires
    events = [{"event": "issue.pr_created", "summary": "PR #42"}]
    result = _build_status_explanation(ctx, events)
    assert "Publishing failed" in result


def test_status_explanation_blocked_publish_failed_uses_publish_event_reason() -> None:
    ctx = _ctx(flow_stage="blocked", labels=("publish-failed",))
    events = [
        {
            "event": "publish.failed",
            "source_event": "publish.failed",
            "summary": "Push failed: ERROR: Test-skipping patterns detected",
        }
    ]

    result = _build_status_explanation(ctx, events)

    assert result == "Publishing failed: Push failed: ERROR: Test-skipping patterns detected"


def test_status_explanation_blocked_invalid_completion_record() -> None:
    ctx = _ctx(flow_stage="blocked", labels=("needs-human",))
    events = [
        {
            "event": "agent.invalid_completion_record",
            "source_event": "session.invalid_completion_record",
            "summary": (
                "Completion record rejected: "
                "follow_up_issues exceeds maximum count (6 > 5)"
            ),
        }
    ]

    result = _build_status_explanation(ctx, events)

    assert result == (
        "Completion record rejected — "
        "follow_up_issues exceeds maximum count (6 > 5)"
    )


def test_status_explanation_blocked_invalid_completion_survives_terminal_failed_event(
    tmp_path,
) -> None:
    ctx = _ctx(flow_stage="blocked", labels=("needs-human",))
    issue_number = 4124
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    completion_path = tmp_path / "completion-agent_backend.json"
    completion_path.write_text('{"outcome":"completed"}\n', encoding="utf-8")
    reason = "Completion record rejected: invalid JSON: line 1"
    events = _project_user_events_from_trace_events(
        issue_number,
        [
            TraceEvent(
                EventName.SESSION_INVALID_COMPLETION_RECORD,
                {
                    "issue_number": issue_number,
                    "task": "code",
                    "run_dir": str(run_dir),
                    "summary": reason,
                    "completion_path_absolute": str(completion_path),
                },
            ),
            TraceEvent(
                EventName.SESSION_FAILED,
                {
                        "issue_number": issue_number,
                        "task": "code",
                        "run_dir": str(run_dir),
                        "reason": reason,
                    "failure_kind": "invalid_completion_record",
                    "completion_parse_error": "invalid JSON: line 1",
                },
            ),
        ],
    )

    assert not any(event.get("event") == "agent.invalid_completion_record" for event in events)
    assert events[-1]["event"] == "agent.failed"
    assert events[-1]["source_event"] == "session.failed"
    assert "failure_kind" not in events[-1]

    assert _build_status_explanation(ctx, events) == (
        "Completion record rejected — invalid JSON: line 1"
    )


def test_status_explanation_awaiting_merge() -> None:
    ctx = _ctx(flow_stage="awaiting_merge", pr_number=4124)
    assert _build_status_explanation(ctx, []) == "PR #4124 approved — ready to merge"


def test_issue_detail_flow_stage_treats_reconciled_pr_history_as_done() -> None:
    state = SimpleNamespace(
        session_history=[
            SessionHistoryEntry(
                issue_number=4124,
                title="Add cache coalescing",
                agent_type="agent:claude",
                status="merged",
                runtime_minutes=12,
                pr_url="https://github.com/org/repo/pull/4124",
            )
        ]
    )

    assert (
        _determine_issue_flow_stage(
            4124,
            (),
            None,
            state,
            "https://github.com/org/repo/pull/4124",
        )
        == "done"
    )


def test_journey_cycles_require_logical_semantics() -> None:
    events = [{"event": "session.started", "timestamp": "2026-02-16T10:00:00Z"}]
    assert _journey_cycles(events, "2026-02-16") == ()


def test_journey_cycles_group_by_logical_run_and_cycle() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:10:00Z", logical_run=1, logical_cycle=1, status="completed"),
        _evt("review.started", timestamp="2026-02-16T10:11:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.changes_requested", timestamp="2026-02-16T10:12:00Z", logical_run=1, logical_cycle=1, task="review", status="failed"),
        _evt("rework.started", timestamp="2026-02-16T10:20:00Z", logical_run=1, logical_cycle=2, task="rework", agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:30:00Z", logical_run=1, logical_cycle=2, status="completed"),
        _evt("session.started", timestamp="2026-02-16T11:00:00Z", logical_run=2, logical_cycle=1, agent="agent:backend"),
    ]

    cycles = _journey_cycles(events, "2026-02-16")
    assert len(cycles) == 3
    assert [c.lifecycle for c in cycles] == [1, 1, 2]
    assert [c.iteration for c in cycles] == [1, 2, 1]


def test_last_run_filter_uses_logical_run() -> None:
    cycles = [
        {"lifecycle": 1, "cycle": 1, "iteration": 1, "run_id": "a"},
        {"lifecycle": 1, "cycle": 2, "iteration": 2, "run_id": "b"},
        {"lifecycle": 2, "cycle": 3, "iteration": 1, "run_id": "c"},
    ]
    filtered = filter_last_run_cycles(cycles)
    assert len(filtered) == 1
    assert filtered[0]["lifecycle"] == 2


def test_rework_cycle_outcome_prefixed_for_non_review_cycle() -> None:
    events = [
        _evt("rework.started", timestamp="2026-02-16T10:20:00Z", logical_run=1, logical_cycle=2, task="rework"),
        _evt("session.failed", timestamp="2026-02-16T10:30:00Z", logical_run=1, logical_cycle=2, status="failed", summary="compile error"),
    ]
    cycles = _journey_cycles(events, "2026-02-16")
    assert cycles[0].outcome.label.startswith("Rework →")
    # Path B (PR #6333): tone is owned by the projection.  A
    # non-review rework cycle whose inner outcome is a session
    # failure is failed-toned.
    assert cycles[0].outcome.tone == "failed"


def test_phase_groups_follow_logical_phase_not_event_name_guessing() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("validation.completed", timestamp="2026-02-16T10:01:00Z", logical_run=1, logical_cycle=1),
        _evt("review.started", timestamp="2026-02-16T10:02:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.approved", timestamp="2026-02-16T10:03:00Z", logical_run=1, logical_cycle=1, task="review"),
    ]
    cycles = _journey_cycles(events, "2026-02-16")
    labels = [group.label for group in cycles[0].phase_groups]
    assert labels == ["Coding", "Orchestrator", "Review"]


def test_collect_cycle_artifacts_extracts_pr_and_review_feedback() -> None:
    events = [
        _evt(
            "issue.pr_created",
            timestamp="2026-02-16T10:00:00Z",
            logical_run=1,
            logical_cycle=1,
            artifacts=[{"type": "pull_request", "label": "PR", "value": "https://github.com/org/repo/pull/4124"}],
        ),
        _evt("review.approved", timestamp="2026-02-16T10:01:00Z", logical_run=1, logical_cycle=1, task="review"),
    ]
    artifacts = collect_cycle_artifacts(events)
    assert artifacts.pr_number == 4124
    assert artifacts.has_review_feedback is True


def test_timeline_steps_preserve_day_field() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-15T10:00:00Z", logical_run=1, logical_cycle=1),
        _evt("session.completed", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, status="completed"),
    ]
    steps = _build_journey_steps(events, "2026-02-16")
    assert [step["day"] for step in steps] == ["2026-02-15", "2026-02-16"]


def test_build_issue_detail_view_model_returns_runs() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:10:00Z", logical_run=1, logical_cycle=1, status="completed"),
        _evt("session.started", timestamp="2026-02-16T11:00:00Z", logical_run=2, logical_cycle=1, agent="agent:backend"),
    ]
    payload = build_issue_detail_view_model(
        issue_number=4057,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
        context=_ctx(flow_stage="in_progress", active_runtime_minutes=1, active_task_kind="code"),
    )
    assert payload["run_count"] == 2
    assert payload["runs"][1]["expanded"] is True


def test_scratch_retry_surfaces_run_and_cycle_labels() -> None:
    events = [
        {
            **_evt(
                "issue.unblocked",
                timestamp="2026-04-29T12:00:00Z",
                logical_run=1,
                logical_cycle=1,
            ),
            "from_scratch": True,
        },
        {
            **_evt(
                "session.started",
                timestamp="2026-04-29T12:01:00Z",
                logical_run=1,
                logical_cycle=1,
                agent="agent:backend",
            ),
            "reset_from_scratch": True,
        },
    ]
    payload = build_issue_detail_view_model(
        issue_number=359,
        title="Scratch retry",
        issue_url="https://github.com/org/repo/issues/359",
        events=events,
        phase_toc=[],
        cycles=[],
        context=_ctx(flow_stage="in_progress"),
    )

    latest_run = payload["runs"][-1]
    assert latest_run["reset_from_scratch"] is True
    assert latest_run["run_label"] == "Run 1 (scratch retry)"
    assert latest_run["cycles"][0]["reset_from_scratch"] is True
    assert latest_run["cycles"][0]["cycle_label"] == "Cycle 1 (scratch)"


def test_latest_run_without_review_events_not_marked_completed() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:05:00Z", logical_run=1, logical_cycle=1, status="completed"),
        _evt("review.started", timestamp="2026-02-16T10:06:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.approved", timestamp="2026-02-16T10:07:00Z", logical_run=1, logical_cycle=1, task="review", status="completed"),
        _evt("rework.started", timestamp="2026-02-16T11:00:00Z", logical_run=2, logical_cycle=1, task="rework", agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T11:10:00Z", logical_run=2, logical_cycle=1, status="completed"),
    ]
    payload = build_issue_detail_view_model(
        issue_number=4064,
        title="Latest run invariant",
        issue_url="https://github.com/org/repo/issues/4064",
        events=events,
        phase_toc=[],
        cycles=[],
        context=_ctx(flow_stage="in_progress"),
    )
    latest = payload["runs"][-1]
    assert "completed" not in str(latest["outcome"]).lower()
    assert "approved" not in str(latest["outcome"]).lower()


def test_step_detail_excludes_artifact_resolution_errors() -> None:
    """Artifact errors (run_dir missing) should not pollute step detail text."""
    events = [
        _evt(
            "session.started",
            timestamp="2026-02-16T10:00:00Z",
            logical_run=1,
            logical_cycle=1,
            agent="agent:backend",
        ),
    ]
    # Simulate what the web layer adds when run_dir is missing
    events[0]["actions_error"] = "run_dir does not exist: /tmp/gone"
    events[0]["actions"] = [
        {
            "type": "show_actions_error",
            "label": "What is missing?",
            "error_message": "run_dir does not exist: /tmp/gone",
            "error_messages": ["run_dir does not exist: /tmp/gone"],
        }
    ]
    steps = _build_journey_steps(events, "2026-02-16")
    assert len(steps) == 1
    assert "detail" not in steps[0], "artifact errors should not appear as step detail"


def test_step_detail_preserves_event_own_detail() -> None:
    """The event's own detail text should still appear."""
    events = [
        _evt(
            "session.completed",
            timestamp="2026-02-16T10:10:00Z",
            logical_run=1,
            logical_cycle=1,
            status="completed",
        ),
    ]
    events[0]["detail"] = "Agent completed successfully"
    events[0]["actions_error"] = "run_dir does not exist: /tmp/gone"
    steps = _build_journey_steps(events, "2026-02-16")
    assert steps[0]["detail"] == "Agent completed successfully"
    assert "run_dir" not in steps[0]["detail"]


def test_validation_retry_creates_separate_cycles() -> None:
    """Each validation-retry iteration should be its own cycle within the run."""
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:05:00Z", logical_run=1, logical_cycle=1, status="completed"),
        _evt("review.started", timestamp="2026-02-16T10:06:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.approved", timestamp="2026-02-16T10:07:00Z", logical_run=1, logical_cycle=1, task="review", status="completed"),
        # Validation fails → retry starts a new cycle
        _evt("session.validation_retry_needed", timestamp="2026-02-16T10:08:00Z", logical_run=1, logical_cycle=2, status="failed"),
        _evt("session.started", timestamp="2026-02-16T10:09:00Z", logical_run=1, logical_cycle=2, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:14:00Z", logical_run=1, logical_cycle=2, status="completed"),
        _evt("review.started", timestamp="2026-02-16T10:15:00Z", logical_run=1, logical_cycle=2, task="review"),
        _evt("review.approved", timestamp="2026-02-16T10:16:00Z", logical_run=1, logical_cycle=2, task="review", status="completed"),
    ]
    cycles = _journey_cycles(events, "2026-02-16")
    assert len(cycles) == 2, f"Expected 2 cycles, got {len(cycles)}"
    assert cycles[0].iteration == 1
    assert cycles[1].iteration == 2


def test_review_exchange_rework_events_surface_coder_step_between_review_rounds() -> None:
    events = [
        _evt("review.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review_exchange.round_started", timestamp="2026-02-16T10:01:00Z", logical_run=1, logical_cycle=1, task="review"),
        {
            **_evt("review.rework_started", timestamp="2026-02-16T10:02:00Z", logical_run=1, logical_cycle=1, task="review", summary="Fix two issues"),
            "logical_phase": "rework",
        },
        {
            **_evt("review.rework_completed", timestamp="2026-02-16T10:03:00Z", logical_run=1, logical_cycle=1, task="review", status="completed"),
            "logical_phase": "rework",
            "detail": "Round 1. Coder response: ok. Applied both fixes.",
        },
        _evt("review_exchange.round_started", timestamp="2026-02-16T10:04:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.approved", timestamp="2026-02-16T10:05:00Z", logical_run=1, logical_cycle=1, task="review", status="completed"),
    ]

    cycles = _journey_cycles(events, "2026-02-16")
    labels = [group.label for group in cycles[0].phase_groups]
    assert labels == ["Review", "Rework", "Review"]
    narratives = [step.narrative for step in cycles[0].steps]
    assert "Coder addressing review feedback: Fix two issues" in narratives
    assert "Coder finished review rework" in narratives[3]


def test_user_story_hides_outer_coding_completion_during_review_round() -> None:
    events = [
        _evt("review_exchange.round_started", timestamp="2026-04-29T00:57:40Z", logical_run=6, logical_cycle=2, task="review"),
        {
            **_evt("agent.coding_completed", timestamp="2026-04-29T00:58:57Z", logical_run=6, logical_cycle=2, task="code", status="completed"),
            "event_intent": "coding",
            "logical_phase": "coding",
            "narrative": "Agent finished coding",
        },
        _evt(
            "review_exchange.round_completed",
            timestamp="2026-04-29T00:59:11Z",
            logical_run=6,
            logical_cycle=2,
            task="review",
            status="completed",
            summary="ok",
        ),
        _evt(
            "review.approved",
            timestamp="2026-04-29T00:59:14Z",
            logical_run=6,
            logical_cycle=2,
            task="review",
            status="completed",
            summary="Looks good.",
        ),
    ]

    story_payload = build_issue_detail_view_model(
        issue_number=361,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/361",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )
    ops_payload = build_issue_detail_view_model(
        issue_number=361,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/361",
        events=events,
        phase_toc=[],
        cycles=[],
        view="ops",
    )

    story_step_events = [step["event"] for step in story_payload["timeline_steps"]]
    ops_step_events = [step["event"] for step in ops_payload["timeline_steps"]]
    assert "agent.coding_completed" not in story_step_events
    assert "agent.coding_completed" in ops_step_events

    latest_cycle = story_payload["runs"][-1]["cycles"][0]
    assert [group["label"] for group in latest_cycle["phase_groups"]] == ["Review"]


def test_user_story_review_exchange_completed_closes_round_before_later_coding_completion() -> None:
    events = [
        _evt("review_exchange.round_started", timestamp="2026-04-29T00:57:40Z", logical_run=6, logical_cycle=2, task="review"),
        _evt(
            "review_exchange.completed",
            timestamp="2026-04-29T00:59:11Z",
            logical_run=6,
            logical_cycle=2,
            task="review",
            status="completed",
            summary="review exchange complete",
        ),
        {
            **_evt("agent.coding_completed", timestamp="2026-04-29T00:59:20Z", logical_run=6, logical_cycle=2, task="code", status="completed"),
            "event_intent": "coding",
            "logical_phase": "coding",
            "narrative": "Agent finished coding",
        },
    ]

    payload = build_issue_detail_view_model(
        issue_number=361,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/361",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    step_events = [step["event"] for step in payload["timeline_steps"]]
    assert step_events == [
        "review_exchange.completed",
        "agent.coding_completed",
    ]


def test_user_story_review_round_projection_uses_source_event_boundaries() -> None:
    events = [
        {
            **_evt("review.started", timestamp="2026-04-29T00:57:40Z", logical_run=6, logical_cycle=2, task="review"),
            "source_event": "review_exchange.round_started",
        },
        {
            **_evt("agent.coding_completed", timestamp="2026-04-29T00:58:57Z", logical_run=6, logical_cycle=2, task="code", status="completed"),
            "source_event": "observation.completion_detected",
            "event_intent": "coding",
            "logical_phase": "coding",
            "narrative": "Agent finished coding",
        },
        _evt(
            "review_exchange.round_completed",
            timestamp="2026-04-29T00:59:11Z",
            logical_run=6,
            logical_cycle=2,
            task="review",
            status="completed",
            summary="ok",
        ),
    ]

    payload = build_issue_detail_view_model(
        issue_number=361,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/361",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    step_events = [step["event"] for step in payload["timeline_steps"]]
    assert "agent.coding_completed" not in step_events


def test_user_story_collapses_initial_review_start_cluster_to_single_step() -> None:
    events = [
        _evt("review.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review_exchange.started", timestamp="2026-02-16T10:00:05Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review_exchange.round_started", timestamp="2026-02-16T10:00:10Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review_exchange.round_completed", timestamp="2026-02-16T10:03:00Z", logical_run=1, logical_cycle=1, task="review", summary="round 1 ok"),
    ]

    payload = build_issue_detail_view_model(
        issue_number=4057,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    step_events = [step["event"] for step in payload["timeline_steps"]]
    assert step_events == [
        "review_exchange.round_completed",
    ]
    assert payload["timeline_steps"][0]["narrative"] == "Reviewed: round 1 ok"

    latest_cycle = payload["runs"][-1]["cycles"][0]
    assert [step["event"] for step in latest_cycle["steps"]] == step_events
    assert latest_cycle["steps"][0]["narrative"] == "Reviewed: round 1 ok"


def test_user_story_collapses_review_success_end_cluster_to_terminal_step() -> None:
    events = [
        _evt("review_exchange.round_started", timestamp="2026-02-16T10:00:10Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review_exchange.round_completed", timestamp="2026-02-16T10:03:00Z", logical_run=1, logical_cycle=1, task="review", summary="round 1 ok"),
        _evt("review_exchange.completed", timestamp="2026-02-16T10:03:05Z", logical_run=1, logical_cycle=1, task="review", summary="1 round complete"),
        _evt("review.approved", timestamp="2026-02-16T10:03:10Z", logical_run=1, logical_cycle=1, task="review", status="completed", summary="Looks good"),
    ]

    payload = build_issue_detail_view_model(
        issue_number=4057,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    step_events = [step["event"] for step in payload["timeline_steps"]]
    assert step_events == [
        "review.approved",
    ]
    assert "Looks good" in payload["timeline_steps"][0]["narrative"]


def test_user_story_collapses_review_changes_requested_end_cluster_to_terminal_step() -> None:
    events = [
        _evt("review_exchange.round_started", timestamp="2026-02-16T10:00:10Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review_exchange.round_completed", timestamp="2026-02-16T10:03:00Z", logical_run=1, logical_cycle=1, task="review", summary="round 1 changes_requested"),
        _evt("review.changes_requested", timestamp="2026-02-16T10:03:10Z", logical_run=1, logical_cycle=1, task="review", status="failed", summary="Fix abstraction"),
    ]

    payload = build_issue_detail_view_model(
        issue_number=4057,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    step_events = [step["event"] for step in payload["timeline_steps"]]
    assert step_events == [
        "review.changes_requested",
    ]


def test_user_story_shows_one_terminal_review_entry_for_completed_exchange() -> None:
    events = [
        {
            **_evt("agent.coding_completed", timestamp="2026-04-29T00:57:30Z", logical_run=6, logical_cycle=2, task="code", status="completed"),
            "event_intent": "coding",
            "logical_phase": "coding",
            "narrative": "Agent finished coding",
        },
        _evt("review.started", timestamp="2026-04-29T00:57:35Z", logical_run=6, logical_cycle=2, task="review"),
        _evt("review_exchange.started", timestamp="2026-04-29T00:57:36Z", logical_run=6, logical_cycle=2, task="review"),
        _evt("review_exchange.round_started", timestamp="2026-04-29T00:57:40Z", logical_run=6, logical_cycle=2, task="review"),
        _evt("review_exchange.role_prompted", timestamp="2026-04-29T00:57:42Z", logical_run=6, logical_cycle=2, task="review"),
        _evt("review_exchange.role_feedback", timestamp="2026-04-29T00:59:00Z", logical_run=6, logical_cycle=2, task="review", summary="ok"),
        _evt("review_exchange.round_completed", timestamp="2026-04-29T00:59:11Z", logical_run=6, logical_cycle=2, task="review", status="completed", summary="ok"),
        _evt("review_exchange.completed", timestamp="2026-04-29T00:59:12Z", logical_run=6, logical_cycle=2, task="review", status="completed", summary="1 round"),
        _evt("review.approved", timestamp="2026-04-29T00:59:14Z", logical_run=6, logical_cycle=2, task="review", status="completed", summary="Looks good."),
    ]

    payload = build_issue_detail_view_model(
        issue_number=361,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/361",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    step_events = [step["event"] for step in payload["timeline_steps"]]
    assert step_events == ["agent.coding_completed", "review.approved"]


def test_user_story_shows_orphan_terminal_review_event_without_start() -> None:
    events = [
        _evt(
            "review.approved",
            timestamp="2026-04-29T00:59:14Z",
            logical_run=6,
            logical_cycle=2,
            task="review",
            status="completed",
            summary="Looks good.",
        ),
    ]

    payload = build_issue_detail_view_model(
        issue_number=361,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/361",
        events=events,
        phase_toc=[],
        cycles=[],
        view="user",
    )

    assert [step["event"] for step in payload["timeline_steps"]] == [
        "review.approved"
    ]
    step = payload["timeline_steps"][0]
    assert "Looks good." in step["narrative"]
    assert step.get("run_id") is None

    latest_cycle = payload["runs"][-1]["cycles"][0]
    assert [step["event"] for step in latest_cycle["steps"]] == [
        "review.approved"
    ]
