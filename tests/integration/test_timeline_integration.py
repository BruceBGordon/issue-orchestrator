"""Integration tests for DB-backed timeline end-to-end behavior."""

from __future__ import annotations

import json
import sqlite3
import base64
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints import web
from issue_orchestrator.events import EventName
from issue_orchestrator.domain.artifact_contracts import (
    AgentProvider,
    AgentRole,
    AgentTurnArtifactScope,
    ChapterSidecarArtifact,
    ExchangeRunId,
    ExistingFile,
    ExistingNonEmptyFile,
    IssueNumber,
    PositiveAttemptIndex,
    PositiveRoundIndex,
    PromptArtifact,
    ReviewResponseArtifact,
    ReviewerTurnCompleted,
    ReviewerTurnStarted,
    TerminalRecordingArtifact,
)
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore, TimelineStoreConfig
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.timeline_evidence import (
    FinalizeTimelineEvidenceCommand,
    TimelineEvidenceIdentity,
)
from issue_orchestrator.execution.timeline_evidence import FileSystemTimelineEvidence

from tests.conftest import MockEventSink, MockSessionRunner, build_test_orchestrator_deps


def _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host):
    """Build a real orchestrator object with SQLite timeline services wired in."""
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    timeline_store = SqliteTimelineStore(
        sample_config.repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=5000, max_total_records=20000),
    )
    timeline_reader = DefaultTimelineReader(timeline_store)
    timeline_writer = DefaultTimelineWriter(timeline_store)
    timeline_evidence = FileSystemTimelineEvidence(
        archive_root=(
            sample_config.repo_root
            / ".issue-orchestrator"
            / "state"
            / "timeline-evidence"
        ),
        timeline_store=timeline_store,
        retention_days=sample_config.session_output_retention_days,
        retention_tier=sample_config.session_output_retention_tier,
    )

    runner = MockSessionRunner()
    runner.plugin.session_exists_override = False

    deps = build_test_orchestrator_deps(
        sample_config,
        mock_repository_host,
        MockEventSink(),
        runner,
        GitWorktreeManager(),
        working_copy=GitWorkingCopy(),
        timeline_reader=timeline_reader,
        timeline_writer=timeline_writer,
        timeline_evidence=timeline_evidence,
    )
    return Orchestrator(config=sample_config, deps=deps), timeline_writer


def _start_run_with_artifacts(
    repo_root: Path,
    *,
    issue_number: int,
    session_name: str,
) -> str:
    """Create a real run directory with required artifacts for strict timeline actions."""
    session_output = FileSystemSessionOutput()
    worktree = repo_root / f"wt-{issue_number}-{session_name}"
    worktree.mkdir(parents=True, exist_ok=True)
    run = session_output.start_run(worktree, session_name, issue_number=issue_number)
    _write_terminal_recording(run.run_dir / "terminal-recording.jsonl", "agent output\n")
    claude_log = run.run_dir / "claude.jsonl"
    claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
    completion_record = run.run_dir / "completion-agent_backend.json"
    completion_record.write_text('{"outcome":"completed"}\n', encoding="utf-8")
    session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
    return str(run.run_dir)


def _phase_group_labels(cycle: dict[str, object]) -> list[str]:
    """Return phase-group labels for a cycle payload."""
    groups = cycle.get("phase_groups")
    if not isinstance(groups, list):
        return []
    return [
        str(group.get("label"))
        for group in groups
        if isinstance(group, dict) and group.get("label")
    ]


def _step_events(cycle: dict[str, object]) -> list[str]:
    """Return step event names for a cycle payload."""
    steps = cycle.get("steps")
    if not isinstance(steps, list):
        return []
    return [
        str(step.get("event"))
        for step in steps
        if isinstance(step, dict) and step.get("event")
    ]


def _step_by_event(cycle: dict[str, object], event_name: str) -> dict[str, object]:
    """Return the single issue-detail step for an event name."""
    steps = cycle.get("steps")
    assert isinstance(steps, list) and steps
    matches = [
        step for step in steps
        if isinstance(step, dict) and step.get("event") == event_name
    ]
    assert len(matches) == 1, f"expected one {event_name} step, got {_step_events(cycle)}"
    return matches[0]


def _single_action(actions: object, **criteria: object) -> dict[str, object]:
    """Return exactly one action matching all criteria."""
    assert isinstance(actions, list) and actions
    matches = [
        action
        for action in actions
        if isinstance(action, dict)
        and all(action.get(key) == value for key, value in criteria.items())
    ]
    assert len(matches) == 1, (
        f"expected exactly one action matching {criteria}, got {actions}"
    )
    return matches[0]


def _latest_run(payload: dict[str, object]) -> dict[str, object]:
    """Return the latest run from issue-detail payload."""
    runs = payload.get("runs")
    assert isinstance(runs, list) and runs, "expected at least one run"
    latest = runs[-1]
    assert isinstance(latest, dict), "run payload must be an object"
    return latest


def _first_cycle(run: dict[str, object]) -> dict[str, object]:
    """Return the first cycle from a run payload."""
    cycles = run.get("cycles")
    assert isinstance(cycles, list) and cycles, "expected at least one cycle"
    first = cycles[0]
    assert isinstance(first, dict), "cycle payload must be an object"
    return first


def _write_terminal_recording(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 0,
                "data_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_issue_detail_reads_from_sqlite_store(sample_config, mock_repository_host):
    """`/api/issue-detail` should project DB timeline data.

    The legacy `/api/timeline/{issue_number}` route was retired in #6421;
    `/api/issue-detail/{issue_number}` is the single contracted surface and
    carries the same projected `events` (visible in the `debug` view).
    """
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    timeline_db = sample_config.repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Timeline DB Integration", labels=["agent:web"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-code-1",
        "run_dir": run_dir_code,
        "completion_path_absolute": str(Path(run_dir_code) / "completion-agent_backend.json"),
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))

    with sqlite3.connect(str(timeline_db)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 4

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        timeline_response = client.get(f"/api/issue-detail/{issue_number}?view=debug")
        assert timeline_response.status_code == 200
        timeline_payload = timeline_response.json()
        assert len(timeline_payload["events"]) == 4
        assert timeline_payload["events"][0]["event"] == "agent.coding_started"
        assert timeline_payload["events"][-1]["event"] == "review.approved"

        issue_detail_response = client.get(f"/api/issue-detail/{issue_number}")
        assert issue_detail_response.status_code == 200
        issue_detail_payload = issue_detail_response.json()
        assert issue_detail_payload["run_count"] == 1
        assert issue_detail_payload["timeline_steps"], "Expected timeline steps from persisted DB events"
        assert issue_detail_payload.get("summary", {}).get("timeline_diagnostic") is None
        latest_run = _latest_run(issue_detail_payload)
        assert latest_run["session_run_ids"] == ["run-code-1", "run-review-1"]
    finally:
        web.set_orchestrator(None)


def test_issue_detail_uses_mocked_pipeline_artifact_refs_from_sqlite(
    sample_config,
    mock_repository_host,
):
    """Fake review pipeline refs should reach issue-detail as exact UI actions.

    This is a non-browser UI integration test: it drives mocked pipeline
    events through the real timeline writer/store/reader and issue-detail
    route, using typed fake artifact refs as the producer-side contract.
    """
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(
        sample_config,
        mock_repository_host,
    )
    issue_number = 4091
    run_id = "fake-review-exchange-run-1"
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root,
        issue_number=issue_number,
        session_name="review-4091-fake-pipeline",
    )
    run_dir_path = Path(run_dir)
    exchange_dir = run_dir_path / "review-exchange"
    turns_dir = exchange_dir / "turns"
    reviewer_dir = run_dir_path / "reviewer"
    prompt_path = turns_dir / "round-1-reviewer-attempt-1.prompt.md"
    response_path = turns_dir / "round-1-reviewer-attempt-1.result.json"
    recording_path = reviewer_dir / "terminal-recording.jsonl"
    chapters_path = reviewer_dir / "chapters.json"
    transcript_path = exchange_dir / "transcript.log"

    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("fake reviewer prompt", encoding="utf-8")
    response_path.write_text(
        '{"kind":"changes_requested","response_text":"tighten this"}\n',
        encoding="utf-8",
    )
    _write_terminal_recording(recording_path, "fake reviewer session output\n")
    chapters_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issue_number": issue_number,
                "exchange_run_id": run_id,
                "role": "reviewer",
                "chapters": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        "[2026-05-08T19:00:00Z] round=1 role=reviewer section=prompt\n"
        "fake reviewer prompt\n",
        encoding="utf-8",
    )
    FileSystemSessionOutput().update_manifest(
        run_dir_path,
        {
            "reviewer_recording": str(recording_path),
            "review_exchange_transcript_path": str(transcript_path),
        },
    )

    scope = AgentTurnArtifactScope(
        issue_number=IssueNumber(issue_number),
        exchange_run_id=ExchangeRunId(run_id),
        round_index=PositiveRoundIndex(1),
        attempt_index=PositiveAttemptIndex(1),
        role=AgentRole.REVIEWER,
        provider=AgentProvider("fake-reviewer"),
    )
    started = ReviewerTurnStarted(
        scope=scope,
        prompt=PromptArtifact(scope=scope, file=ExistingNonEmptyFile(prompt_path)),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(recording_path),
        ),
        chapters=ChapterSidecarArtifact(scope=scope, file=ExistingFile(chapters_path)),
    )
    completed = ReviewerTurnCompleted(
        started=started,
        response=ReviewResponseArtifact(
            scope=scope,
            file=ExistingNonEmptyFile(response_path),
        ),
        response_type="changes_requested",
        response_text="tighten this",
    )
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="Mocked review pipeline artifact refs",
            labels=["agent:reviewer"],
        ),
    ]

    timeline_writer.record(
        TraceEvent(
            EventName.REVIEW_STARTED,
            {
                "issue_number": issue_number,
                "run_id": run_id,
                "run_dir": run_dir,
                "task": "review",
                "agent": "agent:reviewer",
            },
        )
    )
    timeline_writer.record(
        TraceEvent(
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            {
                "issue_number": issue_number,
                "run_id": run_id,
                "run_dir": run_dir,
                "task": "review",
                "agent": "agent:reviewer",
                "round_index": 1,
                "attempt_index": 1,
                "role": "reviewer",
                "artifact_refs": [
                    ref.to_event_artifact() for ref in started.artifact_refs()
                ],
            },
        )
    )
    timeline_writer.record(
        TraceEvent(
            EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
            {
                "issue_number": issue_number,
                "run_id": run_id,
                "run_dir": run_dir,
                "task": "review",
                "agent": "agent:reviewer",
                "round_index": 1,
                "attempt_index": 1,
                "role": "reviewer",
                "response_type": "changes_requested",
                "getting_closer": True,
                "artifact_refs": [
                    ref.to_event_artifact() for ref in completed.artifact_refs()
                ],
            },
        )
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}?view=ops")
        assert response.status_code == 200
        payload = response.json()
        raw_events = payload.get("events")
        assert isinstance(raw_events, list) and raw_events
        prompted_event = [
            event for event in raw_events
            if event.get("event") == "review_exchange.role_prompted"
        ]
        assert len(prompted_event) == 1
        assert prompted_event[0]["role"] == "reviewer"
        assert prompted_event[0]["attempt_index"] == 1
        cycle = _first_cycle(_latest_run(payload))

        prompted = _step_by_event(cycle, "review_exchange.role_prompted")
        prompted_actions = prompted.get("actions")
        assert _single_action(prompted_actions, path=str(prompt_path)) == {
            "type": "open_path",
            "label": "Open Prompt",
            "path": str(prompt_path),
        }
        assert _single_action(prompted_actions, path=str(chapters_path)) == {
            "type": "open_path",
            "label": "Open Replay Chapters",
            "path": str(chapters_path),
        }
        assert _single_action(prompted_actions, type="open_agent_log") == {
            "type": "open_agent_log",
            "label": "View Reviewer Session Recording",
            "issue_number": issue_number,
            "round_index": 1,
            "session_role": "reviewer",
            "run_dir": run_dir,
        }
        assert _single_action(prompted_actions, type="open_review_transcript") == {
            "type": "open_review_transcript",
            "label": "View Review Transcript",
            "issue_number": issue_number,
            "round_index": 1,
            "transcript_role": "reviewer",
            "run_dir": run_dir,
        }

        feedback = _step_by_event(cycle, "review_exchange.role_feedback")
        feedback_actions = feedback.get("actions")
        assert _single_action(feedback_actions, path=str(response_path)) == {
            "type": "open_path",
            "label": "Open Review Response",
            "path": str(response_path),
        }
        assert _single_action(feedback_actions, type="open_agent_log") == {
            "type": "open_agent_log",
            "label": "View Reviewer Session Recording",
            "issue_number": issue_number,
            "round_index": 1,
            "session_role": "reviewer",
            "run_dir": run_dir,
        }
    finally:
        web.set_orchestrator(None)


def test_timeline_store_does_not_migrate_legacy_jsonl_via_api(sample_config, mock_repository_host):
    """Legacy JSONL timeline should be ignored by forward-only SQLite store."""
    issue_number = 4058
    state_dir = sample_config.repo_root / ".issue-orchestrator" / "state"
    legacy_dir = state_dir / "timeline"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / f"issue-{issue_number}.jsonl"
    legacy_records = [
        {
            "event_id": "legacy-1",
            "timestamp": "2026-02-17T10:00:00Z",
            "event": "session.started",
            "data": {
                "issue_number": issue_number,
                "run_id": "legacy-run-1",
                "run_dir": "/tmp/legacy-run-1",
                "rework_cycle": 0,
            },
        },
        {
            "event_id": "legacy-2",
            "timestamp": "2026-02-17T10:10:00Z",
            "event": "session.completed",
            "data": {
                "issue_number": issue_number,
                "run_id": "legacy-run-1",
                "run_dir": "/tmp/legacy-run-1",
                "rework_cycle": 0,
            },
        },
    ]
    legacy_file.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in legacy_records) + "\n",
        encoding="utf-8",
    )

    orch, _timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Legacy Timeline Ignored", labels=["agent:web"]),
    ]

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}?view=debug")
        assert response.status_code == 200
        payload = response.json()
        assert payload["events"] == []
    finally:
        web.set_orchestrator(None)


def test_issue_detail_4057_like_projection_stays_semantically_correct(sample_config, mock_repository_host):
    """4057-like local-loop flow should project coding->review->publish ordering."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    code_run_id = "run-4057-code-1"
    review_run_id = "run-4057-review-1"
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code-main"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-main"
    )
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="UI: Surface provider circuit breaker status",
            labels=["agent:backend", "pr-pending"],
        ),
    ]

    def record(
        event_name: EventName,
        *,
        run_id: str,
        run_dir: str,
        task: str,
        agent: str,
        summary: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "issue_number": issue_number,
            "run_id": run_id,
            "run_dir": run_dir,
            "task": task,
            "agent": agent,
            "rework_cycle": 0,
        }
        if summary:
            payload["summary"] = summary
        if event_name == EventName.SESSION_COMPLETED:
            payload["completion_path_absolute"] = str(Path(run_dir) / "completion-agent_backend.json")
        if extra:
            payload.update(extra)
        timeline_writer.record(TraceEvent(event_name, payload))

    record(
        EventName.SESSION_STARTED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_VALIDATION_PASSED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_COMPLETED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="Implementation completed",
    )
    record(
        EventName.REVIEW_STARTED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.REVIEW_APPROVED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.ISSUE_PR_CREATED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="PR #4124 created",
        extra={"pr_url": "https://github.com/test/repo/pull/4124"},
    )
    record(
        EventName.REVIEW_COMMENT_ADDED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
        summary="Posted review comment",
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()

        assert payload["run_count"] == 1
        run = _latest_run(payload)
        assert run["session_run_ids"] == [code_run_id, review_run_id]
        assert run["outcome"]["label"] == "Approved"
        assert len(run["cycles"]) == 1

        cycle = _first_cycle(run)
        assert cycle["outcome"]["label"] == "Approved"
        assert cycle["session_run_ids"] == [code_run_id, review_run_id]
        assert _phase_group_labels(cycle) == [
            "Coding",
            "Orchestrator",
            "Review",
            "Orchestrator",
        ]
        assert _step_events(cycle) == [
            "agent.coding_started",
            "validation.passed",
            "agent.completed",
            "review.approved",
            "pr.created",
        ]

        # --- Artifact assertions (reviewer finding #2) ---
        artifacts = cycle.get("artifacts")
        assert isinstance(artifacts, dict), "cycle must carry artifacts dict"
        assert artifacts.get("pr_url") is not None, "PR URL must be extracted from issue.pr_created"
        assert artifacts.get("pr_number") is not None, "PR number must be extracted"
        assert artifacts.get("has_review_feedback") is True, "review.approved → has_review_feedback"

        # --- Narrative assertions (fan-out view registry) ---
        steps = cycle.get("steps", [])
        coding_started = next(s for s in steps if s["event"] == "agent.coding_started")
        assert coding_started.get("narrative"), "fan-out narrative must flow to step"

        # review.comment_added is ops-only, check with ops view
        ops_response = client.get(f"/api/issue-detail/{issue_number}?view=ops")
        ops_payload = ops_response.json()
        ops_run = _latest_run(ops_payload)
        ops_cycle = _first_cycle(ops_run)
        review_comment_step = next(
            step for step in ops_cycle["steps"] if step.get("event") == "review.comment_added"
        )
        assert any(
            action.get("type") == "open_review_feedback"
            for action in review_comment_step.get("actions", [])
        )
    finally:
        web.set_orchestrator(None)


def test_issue_detail_latest_run_stays_single_after_pr_pending_removed_and_requeue(
    sample_config,
    mock_repository_host,
):
    """pr-pending label churn during the PR/rework lifecycle must not split a
    continuous issue lifecycle into multiple logical runs. Approved cycle plus a
    follow-up rework round are the same run; the rework simply adds a new cycle."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    code_run_id = "run-4057-code-1"
    review_run_id = "run-4057-review-1"
    requeue_run_id = "run-4057-requeue-1"
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code-split"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-split"
    )
    run_dir_requeue = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="rework-4057-split"
    )
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="UI: Surface provider circuit breaker status",
            labels=["agent:backend", "in-progress", "rework-cycle-1"],
        ),
    ]

    def record(
        event_name: EventName,
        *,
        run_id: str,
        run_dir: str,
        task: str,
        agent: str,
        removed: list[str] | None = None,
        summary: str | None = None,
        rework_cycle: int = 0,
    ) -> None:
        payload: dict[str, object] = {
            "issue_number": issue_number,
            "run_id": run_id,
            "run_dir": run_dir,
            "task": task,
            "agent": agent,
            "rework_cycle": rework_cycle,
        }
        if removed is not None:
            payload["removed"] = removed
        if summary:
            payload["summary"] = summary
        if event_name == EventName.SESSION_COMPLETED:
            payload["completion_path_absolute"] = str(Path(run_dir) / "completion-agent_backend.json")
        timeline_writer.record(TraceEvent(event_name, payload))

    # Run 1: coding + review approved
    record(
        EventName.SESSION_STARTED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_COMPLETED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="Implementation completed",
    )
    record(
        EventName.REVIEW_STARTED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.REVIEW_APPROVED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.ISSUE_PR_CREATED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="PR #4124 created",
    )
    record(
        EventName.REVIEW_COMMENT_ADDED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
        summary="Posted review comment",
    )

    # Routine label mutation: pr-pending removed as part of the PR/rework
    # lifecycle. This MUST NOT split the run.
    record(
        EventName.ISSUE_LABELS_CHANGED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="orchestrator",
        agent="agent:backend",
        removed=["pr-pending"],
    )

    # Follow-up rework: same logical run, additional cycle.
    record(
        EventName.REWORK_STARTED,
        run_id=requeue_run_id,
        run_dir=run_dir_requeue,
        task="rework",
        agent="agent:backend",
        rework_cycle=1,
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()

        assert payload["run_count"] == 1
        only_run = payload["runs"][0]
        latest_run = _latest_run(payload)
        assert only_run is latest_run

        # Cycle 1 is the original approved cycle; cycle 2 is the rework follow-up.
        assert len(latest_run["cycles"]) == 2
        first_cycle = latest_run["cycles"][0]
        rework_cycle = latest_run["cycles"][1]
        assert first_cycle["outcome"]["label"] == "Approved"
        assert rework_cycle["outcome"]["label"] == "In progress"
        assert _phase_group_labels(rework_cycle)[0] == "Rework"
        # Both cycles belong to the same logical run — pr-pending churn does
        # not create a new run, it just adds another cycle to the existing one.
        assert rework_cycle["lifecycle"] == first_cycle["lifecycle"]
    finally:
        web.set_orchestrator(None)


def test_issue_detail_local_loop_review_rounds_split_into_distinct_cycles(
    sample_config,
    mock_repository_host,
):
    """Local-loop review rounds should create distinct cycles within one logical run."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="UI: Surface provider circuit breaker status",
            labels=["agent:backend", "pr-pending"],
        ),
    ]
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-exchange"
    )

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
        "summary": "Implementation completed",
        "completion_path_absolute": str(Path(run_dir_code) / "completion-agent_backend.json"),
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "round_index": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "round_index": 1,
        "reviewer_response_type": "changes_requested",
        "summary": "Round 1 changes_requested",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_REWORK_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-1",
        "run_dir": run_dir_review,
        "task": "rework",
        "agent": "agent:backend",
        "round_index": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_REWORK_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-1",
        "run_dir": run_dir_review,
        "task": "rework",
        "agent": "agent:backend",
        "round_index": 1,
        "coder_response_type": "completed",
        "summary": "Round 1 rework done",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "round_index": 2,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "round_index": 2,
        "reviewer_response_type": "ok",
        "summary": "Round 2 ok",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rounds": 2,
        "status": "ok",
        "summary": "Two rounds complete",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rounds": 2,
        "summary": "Looks good after round 2",
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}?view=ops")
        assert response.status_code == 200
        payload = response.json()
        latest_run = _latest_run(payload)
        assert len(latest_run["cycles"]) == 2
        first_cycle, second_cycle = latest_run["cycles"]

        assert [cycle["iteration"] for cycle in latest_run["cycles"]] == [1, 2]
        assert [cycle["cycle_in_run"] for cycle in latest_run["cycles"]] == [1, 2]
        assert [cycle["outcome"]["label"] for cycle in latest_run["cycles"]] == ["Changes Requested", "Approved"]

        assert _phase_group_labels(first_cycle) == ["Coding", "Orchestrator", "Review"]
        assert _step_events(first_cycle) == [
            "agent.coding_started",
            "agent.completed",
            "review.started",
            "review_exchange.started",
            "review_exchange.round_started",
            "review_exchange.round_completed",
        ]

        assert _phase_group_labels(second_cycle) == ["Rework", "Review"]
        assert _step_events(second_cycle) == [
            "review.rework_started",
            "review.rework_completed",
            "review_exchange.round_started",
            "review_exchange.round_completed",
            "review_exchange.completed",
            "review.approved",
        ]
        assert latest_run["session_run_ids"] == ["run-4057-code-1", "run-4057-review-1", "run-4057-rework-1"]

        user_response = client.get(f"/api/issue-detail/{issue_number}?view=user")
        assert user_response.status_code == 200
        user_latest_run = _latest_run(user_response.json())
        user_first_cycle, user_second_cycle = user_latest_run["cycles"]
        assert _step_events(user_first_cycle) == [
            "agent.coding_started",
            "agent.completed",
            "review_exchange.round_completed",
        ]
        assert user_first_cycle["steps"][2]["narrative"] == (
            "Reviewer requested changes: Round 1 changes_requested (reviewer)"
        )
        assert _step_events(user_second_cycle) == [
            "review.rework_started",
            "review.rework_completed",
            "review.approved",
        ]
    finally:
        web.set_orchestrator(None)


def test_review_approved_after_validation_retry_stays_in_current_cycle(
    sample_config,
    mock_repository_host,
):
    """A later approval must stay in the current retry cycle even if rounds=2."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-exchange"
    )

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-old",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-old",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_CHANGES_REQUESTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-old",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "summary": "Cycle 1 changes requested",
    }))
    timeline_writer.record(TraceEvent(EventName.REWORK_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-old",
        "run_dir": run_dir_code,
        "task": "rework",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-old",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-old",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rounds": 2,
        "summary": "Old approval in cycle 2",
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_VALIDATION_RETRY_NEEDED, {
        "issue_number": issue_number,
        "run_dir": run_dir_code,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-new",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-new",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-new",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rounds": 2,
        "summary": "New approval should stay in cycle 3",
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}?view=ops")
        assert response.status_code == 200
        payload = response.json()
        latest_run = _latest_run(payload)
        assert [cycle["iteration"] for cycle in latest_run["cycles"]] == [1, 2, 3]
        latest_cycle = latest_run["cycles"][-1]

        assert latest_cycle["iteration"] == 3
        assert _step_events(latest_cycle) == [
            "validation.retry",
            "agent.coding_started",
            "review.started",
            "review.approved",
        ]
        assert latest_cycle["outcome"]["label"] == "Approved"
    finally:
        web.set_orchestrator(None)


def test_user_view_shows_validation_retry_transition_after_review_approval(
    sample_config,
    mock_repository_host,
):
    """The user story must show the retry trigger between approval and the failed retry cycle."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-exchange"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="UI: Surface provider circuit breaker status", labels=["agent:backend"])
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rounds": 2,
        "summary": "Approved after 2 rounds",
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_VALIDATION_RETRY_NEEDED, {
        "issue_number": issue_number,
        "run_dir": run_dir_code,
        "validation_reason": "push_branch_validation_failed",
        "validation_error_summary": "push_branch: Push failed: make validate",
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-2",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_FAILED, {
        "issue_number": issue_number,
        "run_id": "run-4057-code-2",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "summary": "Session ended without PR or status update",
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()
        latest_run = _latest_run(payload)
        assert [cycle["iteration"] for cycle in latest_run["cycles"]] == [1, 2]
        assert _step_events(latest_run["cycles"][0]) == [
            "agent.coding_started",
            "review.approved",
        ]
        assert _step_events(latest_run["cycles"][1]) == [
            "validation.retry",
            "agent.coding_started",
            "agent.failed",
        ]
        assert "failed" in str(latest_run["cycles"][1]["outcome"]["label"]).lower()
    finally:
        web.set_orchestrator(None)


def test_issue_detail_outcome_labels_correct_after_fan_out_renames(
    sample_config,
    mock_repository_host,
):
    """Outcome derivation must use source_event, not fan-out display names.

    After fan-out: session.failed → agent.failed, session.timeout → agent.timed_out,
    session.blocked → agent.blocked, session.completed → agent.completed.  The
    outcome logic must still produce correct labels like 'Failed', 'Timed out', etc.
    """
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)

    # --- Scenario A: session.failed should produce "Failed" outcome ---
    issue_failed = 9001
    run_dir_failed = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_failed, session_name="issue-9001-code"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_failed, title="Failed outcome test", labels=["agent:backend"]),
    ]
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_failed,
        "run_id": "run-9001-code",
        "run_dir": run_dir_failed,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_FAILED, {
        "issue_number": issue_failed,
        "run_id": "run-9001-code",
        "run_dir": run_dir_failed,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
        "summary": "No completion record",
    }))

    # --- Scenario B: session.timeout should produce "Timed out" outcome ---
    issue_timeout = 9002
    run_dir_timeout = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_timeout, session_name="issue-9002-code"
    )
    orch.state.cached_queue_issues.append(
        Issue(number=issue_timeout, title="Timeout outcome test", labels=["agent:backend"]),
    )
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_timeout,
        "run_id": "run-9002-code",
        "run_dir": run_dir_timeout,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_TIMEOUT, {
        "issue_number": issue_timeout,
        "run_id": "run-9002-code",
        "run_dir": run_dir_timeout,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
        "summary": "Exceeded 45 min limit",
    }))

    # --- Scenario C: session.blocked should produce "Agent blocked" outcome ---
    issue_blocked = 9003
    run_dir_blocked = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_blocked, session_name="issue-9003-code"
    )
    orch.state.cached_queue_issues.append(
        Issue(number=issue_blocked, title="Blocked outcome test", labels=["agent:backend"]),
    )
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_blocked,
        "run_id": "run-9003-code",
        "run_dir": run_dir_blocked,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_BLOCKED, {
        "issue_number": issue_blocked,
        "run_id": "run-9003-code",
        "run_dir": run_dir_blocked,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
        "reason": "Missing API credentials",
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)

        # Scenario A: "Failed" outcome
        resp_a = client.get(f"/api/issue-detail/{issue_failed}")
        assert resp_a.status_code == 200
        run_a = _latest_run(resp_a.json())
        cycle_a = _first_cycle(run_a)
        assert "Failed" in cycle_a["outcome"]["label"], (
            f"session.failed → agent.failed must produce 'Failed' outcome, got: {cycle_a['outcome']}"
        )
        # The fan-out event name should be agent.failed
        assert "agent.failed" in _step_events(cycle_a)

        # Scenario B: "Timed out" outcome
        resp_b = client.get(f"/api/issue-detail/{issue_timeout}")
        assert resp_b.status_code == 200
        run_b = _latest_run(resp_b.json())
        cycle_b = _first_cycle(run_b)
        assert "Timed out" in cycle_b["outcome"]["label"], (
            f"session.timeout → agent.timed_out must produce 'Timed out' outcome, got: {cycle_b['outcome']}"
        )
        assert "agent.timed_out" in _step_events(cycle_b)

        # Scenario C: "Agent blocked" outcome
        resp_c = client.get(f"/api/issue-detail/{issue_blocked}")
        assert resp_c.status_code == 200
        run_c = _latest_run(resp_c.json())
        cycle_c = _first_cycle(run_c)
        assert "blocked" in cycle_c["outcome"]["label"].lower(), (
            f"session.blocked → agent.blocked must produce 'blocked' outcome, got: {cycle_c['outcome']}"
        )
        assert "agent.blocked" in _step_events(cycle_c)
    finally:
        web.set_orchestrator(None)


def test_run_scoped_artifact_usability_enforces_non_empty_log_and_run_dir(
    sample_config,
    mock_repository_host,
):
    """Timeline actions should only offer run-scoped log actions for usable logs."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4061
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4061-code"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Run-scoped artifact usability", labels=["agent:backend"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4061-code-1",
        "run_dir": run_dir,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        log_response = client.get(f"/api/log/local/{issue_number}?run_dir={run_dir}")
        assert log_response.status_code == 200
        log_payload = log_response.json()
        assert any(len(str(line).strip()) >= 8 for line in log_payload.get("lines", []))

        detail_response = client.get(f"/api/issue-detail/{issue_number}")
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        latest_run = _latest_run(detail_payload)
        cycle = _first_cycle(latest_run)
        steps = cycle.get("steps")
        assert isinstance(steps, list) and steps
        actions = steps[0].get("actions")
        assert isinstance(actions, list) and actions

        run_scoped = [
            action for action in actions
            if action.get("type") in {"open_agent_log", "view_claude_log", "open_orchestrator_log", "open_session_diagnostics"}
        ]
        assert run_scoped, "Expected run-scoped actions for usable run artifacts"
        assert all(action.get("run_dir") == run_dir for action in run_scoped)
    finally:
        web.set_orchestrator(None)


def test_run_scoped_log_action_offered_for_start_event_with_non_empty_terminal_recording(
    sample_config,
    mock_repository_host,
):
    """Start events should offer log action when a non-empty terminal recording exists."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4062
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4062-code"
    )
    Path(run_dir, "terminal-recording.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 0,
                "data_b64": base64.b64encode(b"provider output\n").decode("ascii"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Empty log action guardrail", labels=["agent:backend"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4062-code-1",
        "run_dir": run_dir,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        detail_response = client.get(f"/api/issue-detail/{issue_number}")
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        latest_run = _latest_run(detail_payload)
        cycle = _first_cycle(latest_run)
        steps = cycle.get("steps")
        assert isinstance(steps, list) and steps
        actions = steps[0].get("actions") or []
        assert isinstance(actions, list)
        assert any(action.get("type") == "open_agent_log" for action in actions)
    finally:
        web.set_orchestrator(None)


def test_session_diagnostics_dialog_integration_exposes_existing_paths_and_run_scope(
    sample_config,
    mock_repository_host,
):
    """Diagnostics dialog actions should carry valid filesystem paths and run context."""
    orch, _timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4063
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4063-code"
    )
    run_dir_path = Path(run_dir)
    worktree = run_dir_path.parents[2]
    validation_rel = ".issue-orchestrator/validation/run-4063.json"
    diagnostic_rel = ".issue-orchestrator/diagnostics/run-4063.json"
    validation_abs = worktree / validation_rel
    diagnostic_abs = worktree / diagnostic_rel
    validation_abs.parent.mkdir(parents=True, exist_ok=True)
    diagnostic_abs.parent.mkdir(parents=True, exist_ok=True)
    validation_abs.write_text('{"ok": true}\n', encoding="utf-8")
    diagnostic_abs.write_text('{"diagnostic": "ok"}\n', encoding="utf-8")

    session_output = FileSystemSessionOutput()
    session_output.update_manifest(
        run_dir_path,
        {
            "validation_record_path": validation_rel,
            "diagnostic_path": diagnostic_rel,
        },
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/dialog/session-diagnostics/{issue_number}?run_dir={run_dir}")
        assert response.status_code == 200
        payload = response.json()
        actions = payload.get("actions")
        assert isinstance(actions, list) and actions

        path_actions = [
            action for action in actions
            if action.get("type") == "open_path" and action.get("label") in {"Open Session Dir", "Open Validation", "Open Diagnostic"}
        ]
        assert path_actions, "Expected diagnostics path actions"
        for action in path_actions:
            path_value = action.get("path")
            assert isinstance(path_value, str) and path_value
            assert Path(path_value).exists(), f"Expected action path to exist: {path_value}"

        run_scoped_actions = [
            action for action in actions
            if action.get("type") in {"open_agent_log", "open_orchestrator_log", "view_claude_log"}
        ]
        assert run_scoped_actions
        assert all(action.get("run_dir") == run_dir for action in run_scoped_actions)
    finally:
        web.set_orchestrator(None)


def test_archived_session_inspection_is_local_and_read_only(
    sample_config,
    mock_repository_host,
) -> None:
    """Diagnostics and replay must never reattach mutable external evidence."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(
        sample_config, mock_repository_host
    )
    issue_number = 4065
    worktree = sample_config.repo_root / "wt-4065-archive"
    worktree.mkdir(parents=True)
    output = FileSystemSessionOutput()
    run = output.start_run(worktree, "issue-4065", issue_number=issue_number)
    _write_terminal_recording(
        run.run_dir / "terminal-recording.jsonl", "archived terminal output\n"
    )
    external_log = sample_config.repo_root / "external-claude" / "session.jsonl"
    external_log.parent.mkdir(parents=True)
    original_claude = '{"type":"assistant","content":"archived answer"}\n'
    external_log.write_text(original_claude, encoding="utf-8")
    (run.run_dir / "claude-session.jsonl").symlink_to(external_log)
    (run.run_dir / "claude-session.path").write_text(
        str(external_log), encoding="utf-8"
    )
    orchestrator_tail = run.run_dir / "orchestrator-tail.log"
    orchestrator_tail.write_text("archived orchestrator output\n", encoding="utf-8")
    output.update_manifest(
        run.run_dir,
        {
            "claude_log_path": str(external_log),
            "claude_log_dir": str(external_log.parent),
            "orchestrator_tail": str(orchestrator_tail),
        },
    )
    timeline_writer.record(
        TraceEvent(
            EventName.SESSION_FAILED,
            {
                "issue_number": issue_number,
                "run_id": run.run_id,
                "run_dir": str(run.run_dir),
                "task": "code",
                "agent": "agent:backend",
            },
        )
    )
    owner = orch.deps.timeline_evidence
    owner.finalize_terminal(
        FinalizeTimelineEvidenceCommand(
            identity=TimelineEvidenceIdentity(issue_number, run.run_dir),
            outcome="failed",
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    assert owner.archive_worktree(issue_number, worktree) == 1
    archived = (
        sample_config.repo_root
        / ".issue-orchestrator"
        / "state"
        / "timeline-evidence"
        / str(issue_number)
        / run.run_dir.name
    )
    archived_log = archived / "claude-session.jsonl"
    archived_tail = archived / orchestrator_tail.name
    manifest_before = (archived / "manifest.json").read_bytes()

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        diagnostics = client.get(
            f"/api/dialog/session-diagnostics/{issue_number}",
            params={"run_dir": str(archived)},
        )
        assert diagnostics.status_code == 200
        assert (archived / "manifest.json").read_bytes() == manifest_before
        assert archived_log.is_file() and not archived_log.is_symlink()

        external_log.write_text(
            '{"type":"assistant","content":"changed externally"}\n',
            encoding="utf-8",
        )
        external_log.unlink()
        shutil.rmtree(worktree)

        claude = client.get(
            f"/api/session/claude-log/{issue_number}",
            params={"run_dir": str(archived)},
        )
        terminal = client.get(
            f"/api/session/terminal-recording/{issue_number}",
            params={"run_dir": str(archived)},
        )
        orchestrator_log = client.get(
            f"/api/session/orchestrator-log/{issue_number}",
            params={"run_dir": str(archived)},
        )
        assert claude.status_code == 200
        assert claude.json()["entries"][0]["content"] == "archived answer"
        assert terminal.status_code == 200
        assert terminal.json()["events"]
        assert orchestrator_log.status_code == 200
        assert orchestrator_log.json() == {
            "filtered_log_path": str(archived_tail),
            "full_log_path": None,
            "issue_number": issue_number,
        }
        assert archived_tail.read_text(encoding="utf-8") == (
            "archived orchestrator output\n"
        )
        assert archived_log.read_text(encoding="utf-8") == original_claude
        assert not archived_log.is_symlink()
        assert (archived / "manifest.json").read_bytes() == manifest_before
    finally:
        web.set_orchestrator(None)


def test_unavailable_timeline_evidence_has_no_actions_or_artifact_bypass(
    sample_config,
    mock_repository_host,
) -> None:
    orch, _timeline_writer = _build_orchestrator_with_sqlite_timeline(
        sample_config, mock_repository_host
    )
    issue_number = 4066
    run_dir = Path(
        _start_run_with_artifacts(
            sample_config.repo_root,
            issue_number=issue_number,
            session_name="issue-4066-code",
        )
    )
    orch.deps.timeline_evidence.finalize_terminal(
        FinalizeTimelineEvidenceCommand(
            identity=TimelineEvidenceIdentity(issue_number, run_dir),
            outcome="failed",
            ended_at="2020-01-01T00:00:00+00:00",
        )
    )
    missing_archive = (
        sample_config.repo_root
        / ".issue-orchestrator"
        / "state"
        / "timeline-evidence"
        / str(issue_number)
        / "missing-run"
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        for unavailable_run, expected_text in (
            (run_dir, "Expired — artifacts are no longer available"),
            (missing_archive, "Unavailable — retained artifacts could not be found"),
        ):
            diagnostics = client.get(
                f"/api/dialog/session-diagnostics/{issue_number}",
                params={"run_dir": str(unavailable_run)},
            )
            assert diagnostics.status_code == 200
            payload = diagnostics.json()
            rows = {row["label"]: row["value"] for row in payload["rows"]}
            assert rows["Timeline Evidence"] == expected_text
            assert payload["actions"] == []

            artifact_routes = (
                ("terminal-recording", {}),
                ("claude-log", {}),
                ("review-transcript", {}),
                ("prompt", {}),
                ("orchestrator-log", {}),
                (
                    "review-artifact",
                    {
                        "artifact_path": "review.md",
                        "artifact_type": "report",
                    },
                ),
            )
            for route, extra_params in artifact_routes:
                response = client.get(
                    f"/api/session/{route}/{issue_number}",
                    params={"run_dir": str(unavailable_run), **extra_params},
                )
                assert response.status_code == 410, route
    finally:
        web.set_orchestrator(None)


def test_latest_run_without_review_events_is_not_projected_as_approved_or_completed(
    sample_config,
    mock_repository_host,
):
    """Latest run lacking review events must not appear approved/completed."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4064
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4064-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4064"
    )
    run_dir_rework = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="rework-4064"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Latest run review invariant", labels=["agent:backend"]),
    ]

    # Earlier run with review approval.
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4064-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "completion_path_absolute": str(Path(run_dir_code) / "completion-agent_backend.json"),
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-4064-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))

    # Real run boundary: issue.unblocked is a genuine restart trigger
    # (pr-pending label churn is not — it's a routine PR-lifecycle artifact).
    timeline_writer.record(TraceEvent(EventName.ISSUE_UNBLOCKED, {
        "issue_number": issue_number,
        "run_id": "run-4064-code-1",
        "run_dir": run_dir_code,
        "task": "orchestrator",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REWORK_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-rework-1",
        "run_dir": run_dir_rework,
        "task": "rework",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-rework-1",
        "run_dir": run_dir_rework,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4064-rework-1",
        "run_dir": run_dir_rework,
        "task": "code",
        "agent": "agent:backend",
        "completion_path_absolute": str(Path(run_dir_rework) / "completion-agent_backend.json"),
        "rework_cycle": 1,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()
        assert int(payload.get("run_count") or 0) >= 2
        latest_run = _latest_run(payload)
        latest_outcome = str(((latest_run.get("outcome") or {}) if isinstance(latest_run.get("outcome"), dict) else {}).get("label") or latest_run.get("outcome") or "").lower()
        assert "approved" not in latest_outcome
        assert "completed" not in latest_outcome

        latest_cycle = _first_cycle(latest_run)
        latest_events = _step_events(latest_cycle)
        assert not any(evt.startswith("review.") for evt in latest_events)
    finally:
        web.set_orchestrator(None)
