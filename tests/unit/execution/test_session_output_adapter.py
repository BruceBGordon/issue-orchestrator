"""Unit tests for FileSystemSessionOutput adapter.

Tests for list_runs() and related functionality.
"""

import base64
import json
import threading
import time

import pytest

from issue_orchestrator.infra.claude_jsonl import (
    claude_jsonl_entry_preview_lines,
)
from issue_orchestrator.execution.session_output_adapter import (
    FileSystemSessionOutput,
    INDEX_NAME,
    MANIFEST_NAME,
    REVIEW_EXCHANGE_DIR_NAME,
    REVIEW_EXCHANGE_SUMMARY_NAME,
    VALIDATION_RECORD_NAME,
)
from issue_orchestrator.domain.review_exchange_summary import ReviewExchangeSummaryV1
from issue_orchestrator.domain.session_watchdog import ScheduledSessionWatchdog


def _review_exchange_summary(
    *,
    status: str = "ok",
    reason: str = "reviewer_ok",
    rounds: int = 1,
    response_text: str | None = None,
) -> ReviewExchangeSummaryV1:
    return ReviewExchangeSummaryV1.from_payload(
        {
            "status": status,
            "reason": reason,
            "completed_rounds": rounds,
            "response_text": response_text,
            "timestamp": "2026-02-01T00:00:00+00:00",
        }
    )


def _review_exchange_summary_payload(
    *,
    status: str = "ok",
    reason: str = "reviewer_ok",
    rounds: int = 1,
    response_text: str | None = None,
) -> dict[str, object]:
    return _review_exchange_summary(
        status=status,
        reason=reason,
        rounds=rounds,
        response_text=response_text,
    ).to_payload()


def test_record_scheduled_watchdog_persists_exact_owner_value(tmp_path) -> None:
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(tmp_path, "issue-123", issue_number=123)

    session_output.record_scheduled_watchdog(
        run,
        ScheduledSessionWatchdog(timeout_minutes=92),
    )

    manifest = session_output.read_manifest(run.run_dir)
    assert manifest is not None
    assert manifest["scheduled_outer_watchdog_timeout_minutes"] == 92


class TestListRuns:
    """Tests for the list_runs method."""

    @pytest.fixture
    def session_output(self):
        """Create a FileSystemSessionOutput instance."""
        return FileSystemSessionOutput()

    @pytest.fixture
    def worktree_with_runs(self, tmp_path):
        """Create a worktree with multiple run directories."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create run directories
        run1_dir = sessions_dir / "20260117-100000Z__coding-1"
        run1_dir.mkdir()
        run2_dir = sessions_dir / "20260117-110000Z__review-1"
        run2_dir.mkdir()
        run3_dir = sessions_dir / "20260117-120000Z__coding-2"
        run3_dir.mkdir()

        # Create manifests
        (run1_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "ended_at": "2026-01-17T10:30:00Z",
                    "issue_number": 123,
                    "agent_label": "agent:developer",
                    "outcome": "completed",
                    "validation_passed": True,
                }
            )
        )
        (run2_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "session_name": "review-1",
                    "run_id": "20260117-110000Z",
                    "started_at": "2026-01-17T11:00:00Z",
                    "ended_at": "2026-01-17T11:30:00Z",
                    "issue_number": 123,
                    "agent_label": "agent:reviewer",
                    "outcome": "blocked",
                }
            )
        )
        (run3_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "session_name": "coding-2",
                    "run_id": "20260117-120000Z",
                    "started_at": "2026-01-17T12:00:00Z",
                    "issue_number": 123,
                    "agent_label": "agent:developer",
                    # No ended_at - still in progress
                }
            )
        )

        # Create index
        index = {
            "runs": [
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run1_dir),
                    "agent_label": "agent:developer",
                },
                {
                    "session_name": "review-1",
                    "run_id": "20260117-110000Z",
                    "started_at": "2026-01-17T11:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run2_dir),
                    "agent_label": "agent:reviewer",
                },
                {
                    "session_name": "coding-2",
                    "run_id": "20260117-120000Z",
                    "started_at": "2026-01-17T12:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run3_dir),
                    "agent_label": "agent:developer",
                },
            ]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        return tmp_path

    def test_list_runs_returns_all_runs_sorted_by_time(
        self, session_output, worktree_with_runs
    ):
        """Verify list_runs returns runs sorted by started_at."""
        runs = session_output.list_runs(worktree_with_runs)

        assert len(runs) == 3
        assert runs[0]["session_name"] == "coding-1"
        assert runs[1]["session_name"] == "review-1"
        assert runs[2]["session_name"] == "coding-2"

    def test_list_runs_includes_status_from_manifest(
        self, session_output, worktree_with_runs
    ):
        """Verify list_runs derives status from manifest."""
        runs = session_output.list_runs(worktree_with_runs)

        # First run completed with validation passed
        assert runs[0]["status"] == "completed"
        assert runs[0]["validation_passed"] is True

        # Second run was blocked
        assert runs[1]["status"] == "blocked"

        # Third run is in progress (no ended_at)
        assert runs[2]["status"] == "in_progress"

    def test_list_runs_includes_outcome_and_ended_at(
        self, session_output, worktree_with_runs
    ):
        """Verify list_runs includes outcome and ended_at from manifest."""
        runs = session_output.list_runs(worktree_with_runs)

        assert runs[0]["outcome"] == "completed"
        assert runs[0]["ended_at"] == "2026-01-17T10:30:00Z"

        assert runs[1]["outcome"] == "blocked"
        assert runs[1]["ended_at"] == "2026-01-17T11:30:00Z"

        assert runs[2].get("outcome") is None
        assert runs[2].get("ended_at") is None

    def test_list_runs_returns_empty_for_missing_index(self, session_output, tmp_path):
        """Verify list_runs returns empty list when no index exists."""
        runs = session_output.list_runs(tmp_path)
        assert runs == []

    def test_list_runs_returns_empty_for_empty_index(self, session_output, tmp_path):
        """Verify list_runs returns empty list when index has no runs."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / INDEX_NAME).write_text(json.dumps({"runs": []}))

        runs = session_output.list_runs(tmp_path)
        assert runs == []

    def test_list_runs_skips_missing_run_dirs(self, session_output, tmp_path):
        """Verify list_runs skips entries where run_dir doesn't exist."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create one real run
        run1_dir = sessions_dir / "20260117-100000Z__coding-1"
        run1_dir.mkdir()
        (run1_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "session_name": "coding-1",
                    "started_at": "2026-01-17T10:00:00Z",
                    "ended_at": "2026-01-17T10:30:00Z",
                    "outcome": "completed",
                }
            )
        )

        # Index references a non-existent run
        index = {
            "runs": [
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "run_dir": str(run1_dir),
                },
                {
                    "session_name": "review-1",
                    "run_id": "20260117-110000Z",
                    "started_at": "2026-01-17T11:00:00Z",
                    "run_dir": str(sessions_dir / "nonexistent"),
                },
            ]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        runs = session_output.list_runs(tmp_path)
        assert len(runs) == 1
        assert runs[0]["session_name"] == "coding-1"

    def test_list_runs_derives_validation_failed_status(self, session_output, tmp_path):
        """Verify validation_passed=False leads to validation_failed status.

        When the manifest has validation_passed=False, _derive_run_status
        should return 'validation_failed' even if outcome is 'completed'.
        This ensures the UI shows the correct status for failed validation.
        """
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create run with validation_passed=False (validation failed case)
        run_dir = sessions_dir / "20260117-100000Z__coding-1"
        run_dir.mkdir()
        (run_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "ended_at": "2026-01-17T10:30:00Z",
                    "issue_number": 123,
                    "agent_label": "agent:developer",
                    "outcome": "completed",  # Agent reported completed
                    "validation_passed": False,  # But validation failed
                    "validation_failure_reason": "make validate failed",
                }
            )
        )

        # Create index
        index = {
            "runs": [
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run_dir),
                    "agent_label": "agent:developer",
                }
            ]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        runs = session_output.list_runs(tmp_path)

        assert len(runs) == 1
        # Status should be validation_failed, not completed
        assert runs[0]["status"] == "validation_failed"
        assert runs[0]["outcome"] == "completed"
        assert runs[0]["validation_passed"] is False

    def test_list_runs_derives_timed_out_status(self, session_output, tmp_path):
        """Verify current timed_out manifests render as timed-out runs."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        run_dir = sessions_dir / "20260117-100000Z__coding-1"
        run_dir.mkdir()
        (run_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "ended_at": "2026-01-17T10:30:00Z",
                    "issue_number": 123,
                    "agent_label": "agent:developer",
                    "outcome": "timed_out",
                }
            )
        )

        index = {
            "runs": [
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run_dir),
                    "agent_label": "agent:developer",
                }
            ]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        runs = session_output.list_runs(tmp_path)

        assert len(runs) == 1
        assert runs[0]["status"] == "timed_out"
        assert runs[0]["outcome"] == "timed_out"


class TestReviewExchangeSummary:
    """Tests for review exchange summary persistence."""

    @pytest.fixture
    def session_output(self):
        """Create a FileSystemSessionOutput instance."""
        return FileSystemSessionOutput()

    def test_store_review_exchange_summary_writes_manifest_and_validation(
        self, session_output, tmp_path
    ):
        worktree = tmp_path
        summary = _review_exchange_summary(rounds=2)
        review_run = session_output.start_review_exchange_run(
            worktree,
            issue_number=1,
            parent_session_name="issue-1",
            agent_label="agent:coder",
        )
        review_run.assets.validation_record_path.write_text(
            json.dumps({"passed": True})
        )

        stored = session_output.store_review_exchange_summary(
            review_run,
            summary,
        )

        run_dir = review_run.assets.run_dir
        summary_path = run_dir / REVIEW_EXCHANGE_DIR_NAME / REVIEW_EXCHANGE_SUMMARY_NAME
        assert summary_path.exists()
        assert json.loads(summary_path.read_text()) == summary.to_payload()
        assert stored.summary == summary
        assert stored.summary_path == summary_path
        assert stored.exchange_dir == summary_path.parent
        assert stored.validation_record_path == run_dir / VALIDATION_RECORD_NAME
        assert (run_dir / VALIDATION_RECORD_NAME).exists()

        manifest = session_output.read_manifest(run_dir)
        assert manifest is not None
        assert manifest.get("review_exchange_dir") == str(summary_path.parent)

    def test_load_review_exchange_summary_returns_none_when_missing(
        self, session_output, tmp_path
    ):
        assert (
            session_output.load_review_exchange_summary(tmp_path, "missing-session")
            is None
        )

    def test_load_review_exchange_summary_returns_none_for_invalid_json(
        self, session_output, tmp_path
    ):
        worktree = tmp_path
        session_name = "issue-2"
        run_dir = session_output.ensure_run_dir(worktree, session_name)
        exchange_dir = run_dir / REVIEW_EXCHANGE_DIR_NAME
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME).write_text("not-json")

        assert (
            session_output.load_review_exchange_summary(worktree, session_name) is None
        )

    def test_load_review_exchange_summary_falls_back_to_newest_run_with_summary(
        self, session_output, tmp_path
    ):
        import time

        worktree = tmp_path
        session_name = "issue-3"

        older_run = session_output.start_run(worktree, session_name, issue_number=3)
        older_exchange = older_run.run_dir / REVIEW_EXCHANGE_DIR_NAME
        older_exchange.mkdir(parents=True, exist_ok=True)
        (older_exchange / REVIEW_EXCHANGE_SUMMARY_NAME).write_text(
            json.dumps(
                _review_exchange_summary_payload(
                    status="error",
                    reason="reviewer_no_completion",
                    rounds=2,
                    response_text="stale",
                )
            )
        )
        session_output.update_manifest(
            older_run.run_dir,
            {"review_exchange_dir": str(older_exchange)},
        )

        # Ensure a distinct run_id directory for the latest run.
        time.sleep(1.1)
        newer_run = session_output.start_run(worktree, session_name, issue_number=3)
        # Newest run has no summary yet.
        assert newer_run.run_dir.exists()

        loaded = session_output.load_review_exchange_summary(worktree, session_name)
        assert loaded is not None
        assert loaded.summary.response_text == "stale"
        assert loaded.exchange_dir == older_exchange

    def test_load_review_exchange_summary_falls_back_to_dedicated_review_run(
        self, session_output, tmp_path
    ):
        worktree = tmp_path
        coding_session = "coding-1"
        session_output.ensure_run_dir(worktree, coding_session)

        review_run = session_output.start_run(
            worktree,
            "review-exchange-3-20260318T000000000000Z",
            issue_number=3,
        )
        review_exchange_dir = review_run.run_dir / REVIEW_EXCHANGE_DIR_NAME
        review_exchange_dir.mkdir(parents=True, exist_ok=True)
        summary = _review_exchange_summary(response_text="cached")
        (review_exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME).write_text(
            json.dumps(summary.to_payload())
        )
        session_output.update_manifest(
            review_run.run_dir,
            {"review_exchange_dir": str(review_exchange_dir)},
        )

        loaded = session_output.load_review_exchange_summary(worktree, coding_session)
        assert loaded is not None
        assert loaded.summary == summary
        assert loaded.exchange_dir == review_exchange_dir

    def test_load_review_exchange_summary_respects_started_at_boundary(
        self, session_output, tmp_path
    ):
        import time

        worktree = tmp_path
        coding_session = "coding-1"
        old_run = session_output.start_run(
            worktree,
            "review-exchange-3-old",
            issue_number=3,
        )
        old_exchange = old_run.run_dir / REVIEW_EXCHANGE_DIR_NAME
        old_exchange.mkdir(parents=True, exist_ok=True)
        (old_exchange / REVIEW_EXCHANGE_SUMMARY_NAME).write_text(
            json.dumps(_review_exchange_summary_payload(response_text="old"))
        )
        session_output.update_manifest(
            old_run.run_dir, {"review_exchange_dir": str(old_exchange)}
        )

        time.sleep(1.1)
        boundary_run = session_output.start_run(
            worktree, coding_session, issue_number=3
        )
        boundary = session_output.read_manifest(boundary_run.run_dir)["started_at"]

        assert (
            session_output.load_review_exchange_summary(
                worktree,
                coding_session,
                not_before_started_at=boundary,
            )
            is None
        )

        time.sleep(1.1)
        new_run = session_output.start_run(
            worktree,
            "review-exchange-3-new",
            issue_number=3,
        )
        new_exchange = new_run.run_dir / REVIEW_EXCHANGE_DIR_NAME
        new_exchange.mkdir(parents=True, exist_ok=True)
        (new_exchange / REVIEW_EXCHANGE_SUMMARY_NAME).write_text(
            json.dumps(_review_exchange_summary_payload(response_text="new"))
        )
        session_output.update_manifest(
            new_run.run_dir, {"review_exchange_dir": str(new_exchange)}
        )

        loaded = session_output.load_review_exchange_summary(
            worktree,
            coding_session,
            not_before_started_at=boundary,
        )

        assert loaded is not None
        assert loaded.summary.response_text == "new"
        assert loaded.exchange_dir == new_exchange


class TestRunRetentionMetadata:
    """Tests for retention metadata persisted in run manifests."""

    def test_start_run_writes_retention_metadata(self, tmp_path):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(
            tmp_path,
            "issue-123",
            issue_number=123,
            retention_tier="cold",
            retention_days=14,
            retention_pinned=True,
        )

        manifest = json.loads((run.run_dir / MANIFEST_NAME).read_text())
        assert manifest["retention_tier"] == "cold"
        assert manifest["retention_days"] == 14
        assert manifest["retention_pinned"] is True
        assert "retention_expires_at" in manifest


class TestSessionLogCleaning:
    @staticmethod
    def _decoded_recording(path):
        chunks = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            if event.get("event_type") != "output":
                continue
            chunks.append(base64.b64decode(event["data_b64"]).decode("utf-8"))
        return "".join(chunks)

    def test_append_cleaned_session_log_filters_noise_at_write_time(self, tmp_path):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "issue-123", issue_number=123)
        recording = run.run_dir / "terminal-recording.jsonl"

        session_output.append_cleaned_session_log(
            run.run_dir,
            "Line one\n\n✶ Thinking…\nLine two\nRecentactivity\n",
            header="[2026-03-12T12:00:00Z] round=1 role=reviewer section=prompt\n",
        )

        assert self._decoded_recording(recording) == (
            "[2026-03-12T12:00:00Z] round=1 role=reviewer section=prompt\n"
            "Line one\n"
            "Line two\n\n"
        )

    def test_append_cleaned_session_log_skips_orphan_header_when_body_is_only_noise(
        self, tmp_path
    ):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "issue-123", issue_number=123)
        recording = run.run_dir / "terminal-recording.jsonl"

        session_output.append_cleaned_session_log(
            run.run_dir,
            "✶ Thinking…\nRecentactivity\n",
            header="[2026-03-12T12:00:00Z] round=1 role=reviewer section=prompt\n",
        )

        assert recording.read_text(encoding="utf-8") == ""


class TestClaudeLogAttachment:
    def test_claude_jsonl_parser_keeps_preview_tool_summaries_stable(self):
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Investigating"},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "src/app.py"},
                    },
                    {"type": "tool_use", "name": "TodoWrite", "input": "not-a-dict"},
                ]
            },
        }

        assert claude_jsonl_entry_preview_lines(entry) == [
            "Investigating",
            "Read: src/app.py",
            "TodoWrite",
        ]

    def test_attach_claude_log_serializes_manifest_writes(self, tmp_path, monkeypatch):
        session_output = FileSystemSessionOutput()
        claude_dir = tmp_path / "claude-project"
        claude_dir.mkdir()
        (claude_dir / "session.jsonl").write_text(
            json.dumps({"timestamp": "2026-03-18T12:00:00Z", "sessionId": "sess-1"})
            + "\n",
            encoding="utf-8",
        )
        run = session_output.start_run(
            tmp_path,
            "issue-123",
            issue_number=123,
            claude_log_dir=str(claude_dir),
        )

        original_write_json = FileSystemSessionOutput._write_json  # noqa: SLF001
        stats = {"active": 0, "max_active": 0}
        stats_lock = threading.Lock()

        def tracked_write_json(path, payload):
            with stats_lock:
                stats["active"] += 1
                stats["max_active"] = max(stats["max_active"], stats["active"])
            time.sleep(0.02)
            try:
                original_write_json(path, payload)
            finally:
                with stats_lock:
                    stats["active"] -= 1

        monkeypatch.setattr(
            FileSystemSessionOutput,
            "_write_json",
            staticmethod(tracked_write_json),
        )

        update_thread = threading.Thread(
            target=session_output.update_manifest,
            args=(
                run.run_dir,
                {"diagnostic_path": str(run.run_dir / "diagnostic.json")},
            ),
        )
        attach_thread = threading.Thread(
            target=session_output.attach_claude_log_for_run,
            args=(run.run_dir,),
        )
        update_thread.start()
        attach_thread.start()
        update_thread.join()
        attach_thread.join()

        manifest = session_output.read_manifest(run.run_dir)
        assert manifest is not None
        assert manifest["diagnostic_path"].endswith("diagnostic.json")
        assert manifest["claude_log_path"].endswith("session.jsonl")
        assert stats["max_active"] == 1

    def test_attach_claude_log_does_not_backfill_terminal_recording(self, tmp_path):
        session_output = FileSystemSessionOutput()
        claude_dir = tmp_path / "claude-project"
        claude_dir.mkdir()
        run = session_output.start_run(
            tmp_path,
            "issue-123",
            issue_number=123,
            claude_log_dir=str(claude_dir),
        )
        claude_log = claude_dir / "session.jsonl"
        claude_log.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "stream_event",
                            "event": {
                                "type": "content_block_delta",
                                "delta": {"type": "text_delta", "text": "Hello"},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {"type": "text", "text": " world"},
                                    {
                                        "type": "tool_use",
                                        "name": "Bash",
                                        "input": {"command": "pwd"},
                                    },
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        attached = session_output.attach_claude_log(run.run_dir)

        assert attached == claude_log
        recording = run.run_dir / "terminal-recording.jsonl"
        assert recording.read_text(encoding="utf-8") == ""
