from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TIMELINE_SCHEMA_VERSION, TimelineStream
from issue_orchestrator.domain.pause_state import PauseState


def _create_mock_orchestrator(issue_number: int, title: str) -> MagicMock:
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.queue_refresh_seconds = 600
    config.ui_mode = "web"
    config.web_port = 8080
    config.filtering.label = None
    config.filtering.milestone = None
    config.config_path = Path("/tmp/config.yaml")
    config.repo_root = Path("/tmp/repo")
    config.worktree_base = Path("/tmp/worktrees")
    config.agents = {
        "agent:web": AgentConfig(
            prompt_path=Path("/tmp/prompt.txt"),
            model="sonnet",
            timeout_minutes=45,
        ),
    }

    mock_orch = MagicMock()
    mock_orch.config = config
    mock_orch.state = OrchestratorState(
        active_sessions=[],
        session_history=[],
        completed_today=[],
        pause_state=PauseState.running(),
        priority_queue=[],
        startup_status="complete",
        cached_queue_issues=[Issue(number=issue_number, title=title, labels=["agent:web"])],
    )
    mock_orch.pause = MagicMock()
    mock_orch.resume = MagicMock()
    mock_orch.request_shutdown = MagicMock()
    mock_orch.shutdown_requested = False

    mock_deps = MagicMock()
    mock_deps.publish_recovery = MagicMock()
    mock_deps.publish_recovery.can_retry_publish.return_value = False
    mock_deps.timeline_reader = MagicMock()
    mock_orch.deps = mock_deps
    mock_orch.scheduler = MagicMock()
    mock_orch.scheduler.sort_by_priority.side_effect = lambda issues: issues
    mock_orch.scheduler.dependency_evaluator = None
    mock_orch.repository_host = MagicMock()
    mock_orch.repository_host.get_issue.return_value = None
    mock_orch.repository_host.update_label_cache = MagicMock()
    return mock_orch


def _terminal_recording_line(text: str) -> str:
    payload = {
        "event_type": "output",
        "offset_ms": 0,
        "data_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "schema_version": 1,
    }
    return json.dumps(payload) + "\n"


@dataclass(frozen=True)
class RenderedIssueDetail:
    payload: dict[str, Any]

    def assert_step_events(self, *expected: str) -> None:
        assert self.step_events() == list(expected)

    def assert_narrative(self, event_name: str, narrative: str) -> None:
        assert self.step(event_name)["narrative"] == narrative

    def assert_phase_scoped_review_artifacts(
        self,
        *,
        event_name: str,
        round_index: int,
        session_role: str,
        transcript_role: str,
    ) -> None:
        session_action = self.action(event_name, "open_agent_log")
        assert session_action["round_index"] == round_index
        assert session_action["session_role"] == session_role
        transcript_action = self.action(event_name, "open_review_transcript")
        assert transcript_action["round_index"] == round_index
        assert transcript_action["transcript_role"] == transcript_role

    def step_events(self) -> list[str]:
        return [str(step["event"]) for step in self._latest_cycle()["steps"]]

    def step(self, event_name: str) -> dict[str, Any]:
        matches = [
            step for step in self._latest_cycle()["steps"]
            if str(step.get("event") or "") == event_name
        ]
        assert len(matches) == 1, f"expected one {event_name} step, found {self.step_events()}"
        return matches[0]

    def action(self, event_name: str, action_type: str) -> dict[str, Any]:
        actions = self.step(event_name).get("actions") or []
        matches = [action for action in actions if str(action.get("type") or "") == action_type]
        assert matches, f"missing {action_type} on {event_name}: {actions}"
        assert len(matches) == 1, f"expected one {action_type} on {event_name}: {actions}"
        return matches[0]

    def _latest_cycle(self) -> dict[str, Any]:
        runs = self.payload["runs"]
        assert isinstance(runs, list) and runs
        latest_run = runs[-1]
        cycles = latest_run["cycles"]
        assert isinstance(cycles, list) and cycles
        return cycles[-1]


@dataclass(frozen=True)
class ReviewTimelineScenario:
    issue_number: int
    run_dir: Path

    @classmethod
    def create(cls, tmp_path: Path, *, issue_number: int = 4057) -> "ReviewTimelineScenario":
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / f"wt-review-{issue_number}"
        worktree.mkdir(parents=True, exist_ok=True)
        run = session_output.start_run(worktree, f"review-{issue_number}", issue_number=issue_number)
        return cls(issue_number=issue_number, run_dir=run.run_dir)

    def with_reviewer_round(self, *, round_index: int, text: str | None = None) -> "ReviewTimelineScenario":
        return self._with_round_artifacts(
            round_index=round_index,
            role="reviewer",
            text=text or f"reviewer round {round_index}\n",
        )

    def with_coder_round(self, *, round_index: int, text: str | None = None) -> "ReviewTimelineScenario":
        return self._with_round_artifacts(
            round_index=round_index,
            role="coder",
            text=text or f"coder round {round_index}\n",
        )

    def review_started(self) -> TimelineRecord:
        return self._record("review.started", timestamp="2026-03-22T13:34:30Z")

    def review_exchange_started(self) -> TimelineRecord:
        return self._record("review_exchange.started", timestamp="2026-03-22T13:34:31Z")

    def review_round_started(self, *, round_index: int) -> TimelineRecord:
        return self._record(
            "review_exchange.round_started",
            timestamp="2026-03-22T13:34:32Z",
            round_index=round_index,
        )

    def review_round_completed(self, *, round_index: int) -> TimelineRecord:
        return self._record(
            "review_exchange.round_completed",
            timestamp="2026-03-22T13:50:02Z",
            round_index=round_index,
        )

    def review_role_prompted(
        self,
        *,
        round_index: int,
        role: str,
        attempt_index: int = 1,
        artifact_refs: list[dict[str, str]] | None = None,
        timestamp: str = "2026-03-22T13:34:33Z",
    ) -> TimelineRecord:
        return self._record(
            "review_exchange.role_prompted",
            timestamp=timestamp,
            round_index=round_index,
            role=role,
            attempt_index=attempt_index,
            artifact_refs=artifact_refs,
        )

    def review_role_feedback(
        self,
        *,
        round_index: int,
        role: str,
        response_type: str | None = None,
        attempt_index: int = 1,
        timestamp: str = "2026-03-22T13:36:00Z",
    ) -> TimelineRecord:
        return self._record(
            "review_exchange.role_feedback",
            timestamp=timestamp,
            round_index=round_index,
            role=role,
            attempt_index=attempt_index,
            response_type=response_type,
        )

    def review_exchange_completed(self) -> TimelineRecord:
        return self._record("review_exchange.completed", timestamp="2026-03-22T13:50:03Z")

    def review_rework_started(self, *, round_index: int) -> TimelineRecord:
        return self._record(
            "review.rework_started",
            timestamp="2026-03-22T13:45:00Z",
            round_index=round_index,
        )

    def review_approved(self, *, rounds: int) -> TimelineRecord:
        return self._record(
            "review.approved",
            timestamp="2026-03-22T13:50:04Z",
            rounds=rounds,
        )

    def review_changes_requested(self, *, rounds: int) -> TimelineRecord:
        return self._record(
            "review.changes_requested",
            timestamp="2026-03-22T13:50:04Z",
            rounds=rounds,
        )

    def render_issue_detail(
        self,
        *records: TimelineRecord,
        title: str = "Review timeline seam",
        view: str = "user",
    ) -> RenderedIssueDetail:
        mock_orch = _create_mock_orchestrator(self.issue_number, title)
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream.from_records(
            self.issue_number,
            list(records),
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/issue-detail/{self.issue_number}?view={view}")
            assert response.status_code == 200
            return RenderedIssueDetail(response.json())
        finally:
            set_orchestrator(None)

    def _record(
        self,
        event_name: str,
        *,
        timestamp: str,
        round_index: int | None = None,
        rounds: int | None = None,
        role: str | None = None,
        attempt_index: int | None = None,
        artifact_refs: list[dict[str, str]] | None = None,
        response_type: str | None = None,
    ) -> TimelineRecord:
        data: dict[str, Any] = {
            "issue_number": self.issue_number,
            "run_dir": str(self.run_dir),
            "task": "review",
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "review_oriented": True,
            "event_intent": "review",
            "logical_run": 1,
            "logical_cycle": 1,
            "logical_phase": "review",
        }
        if round_index is not None:
            data["round_index"] = round_index
        if rounds is not None:
            data["rounds"] = rounds
        if role is not None:
            data["role"] = role
        if attempt_index is not None:
            data["attempt_index"] = attempt_index
        if artifact_refs is not None:
            data["artifact_refs"] = artifact_refs
        if response_type is not None:
            data["response_type"] = response_type
        return TimelineRecord(
            event_id=f"{event_name}-{role or ''}-{round_index or rounds or 'event'}",
            timestamp=timestamp,
            event=event_name,
            data=data,
        )

    def _with_round_artifacts(
        self,
        *,
        round_index: int,
        role: str,
        text: str,
    ) -> "ReviewTimelineScenario":
        recording_path = (
            self.run_dir
            / "review-exchange"
            / f"round-{round_index:03d}"
            / role
            / "terminal-recording.jsonl"
        )
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        recording_path.write_text(_terminal_recording_line(text), encoding="utf-8")

        # Materialize the legacy transcript.log directly: the persistent
        # runner no longer writes it (chapters.json replaces it), but
        # several pre-existing UI/web-route tests still scan for the
        # pre-cutover artifact layout. The fixture below imitates that
        # layout for those tests; the production runner does not.
        exchange_dir = self.run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = exchange_dir / "transcript.log"
        existing = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
        existing += (
            f"[2026-03-22T13:50:04Z] round={round_index} role={role} section=prompt\n"
            f"{text}"
        )
        transcript_path.write_text(existing, encoding="utf-8")
        # The web route resolves the transcript via the manifest entry —
        # add it so the legacy UI tests still find the file.
        FileSystemSessionOutput().update_manifest(
            self.run_dir,
            {"review_exchange_transcript_path": str(transcript_path)},
        )
        return self
