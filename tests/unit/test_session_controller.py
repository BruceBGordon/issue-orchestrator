"""Tests for SessionController - the core decision logic.

These tests verify the observer/controller separation:
- Given observations + completion records -> correct decisions

No external mocking needed - pure logic tests.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from datetime import datetime
from issue_orchestrator.events import EventName
from issue_orchestrator.control.session_controller import (
    SessionController,
    SessionDecision,
)
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.completion_types import ProcessingResult
from issue_orchestrator.control.completion_record_validation import (
    CompletionRecordLoadFailure,
    CompletionRecordLoadResult,
    WorktreeValidationFailure,
    WorktreeValidationResult,
)
from issue_orchestrator.domain.completion_finalization import (
    CompletionFinalizationCommand,
    CompletionRuntimeState,
    ReviewExchangeRunningQuery,
    decide_completion_finalization,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.provider_resilience import (
    ProviderStatus,
    now_iso,
    write_provider_status,
)
from issue_orchestrator.observation.observation import (
    SessionObservation,
    SessionObservationResult,
)
from issue_orchestrator.domain.models import (
    SessionStatus,
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.provider_resilience import ProviderErrorType
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from tests.callback_endpoint_helpers import ready_callback_endpoint


class StubWorkingCopy:
    """Stub WorkingCopy for SessionController tests.

    SessionController only uses get_head_sha for validation caching.
    These tests don't exercise validation, so we just return a fixed SHA.
    """

    def get_head_sha(self, worktree: Path) -> str | None:
        return "abc1234567890"

    def get_current_branch(self, worktree: Path) -> str | None:
        return "test-branch"


def make_record(outcome: CompletionOutcome, **kwargs) -> CompletionRecord:
    """Helper to create a CompletionRecord with required fields."""
    return CompletionRecord(
        session_id=kwargs.get("session_id", "test"),
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary=kwargs.get("summary", "Test summary"),
        requested_actions=kwargs.get("requested_actions", []),
        implementation=kwargs.get("implementation"),
        problems=kwargs.get("problems"),
        blocked_reason=kwargs.get("blocked_reason"),
        blocked_by=kwargs.get("blocked_by"),
        attempted=kwargs.get("attempted"),
        when_unblocked=kwargs.get("when_unblocked"),
        question=kwargs.get("question"),
        context=kwargs.get("context"),
        options=kwargs.get("options"),
        default_action=kwargs.get("default_action"),
        review_summary=kwargs.get("review_summary"),
        review_issues=kwargs.get("review_issues"),
        risk_level=kwargs.get("risk_level"),
        checks_passed=kwargs.get("checks_passed"),
        checks_needed=kwargs.get("checks_needed"),
        comment_body=kwargs.get("comment_body"),
        validation_record_path=kwargs.get("validation_record_path"),
    )


def make_session_run_assets(
    worktree: Path,
    session_name: str,
    session_output: FileSystemSessionOutput,
) -> SessionRunAssets:
    """Allocate the run contract the active-session owner would inject."""
    worktree = worktree.resolve()
    worktree.mkdir(parents=True, exist_ok=True)
    return session_output.start_run(
        worktree,
        session_name,
        issue_number=123,
        agent_label="agent:test",
        backend="subprocess",
    )


def decide_with_run_assets(
    controller: SessionController,
    *,
    worktree_path: Path,
    session_name: str,
    **kwargs,
) -> SessionDecision:
    """Call the controller with an explicit typed run contract."""
    return controller.decide_outcome(
        worktree_path=worktree_path,
        session_name=session_name,
        session_run_assets=make_session_run_assets(
            worktree_path,
            session_name,
            controller.session_output,
        ),
        **kwargs,
    )


class MockCompletionProcessor:
    """Fake completion processor for testing decisions without I/O."""

    def __init__(self):
        self.completion_record: CompletionRecord | None = None
        self.completion_load_result: CompletionRecordLoadResult | None = None
        self.process_calls: list[dict] = []
        self.worktree_state_valid = True
        self.worktree_state_reason = ""
        self.dirty_policy_results: list[WorktreeValidationResult] = []
        self.process_result = ProcessingResult(
            success=True,
            message="Processed completion",
            pr_url="https://github.com/test/repo/pull/1",
        )
        self.review_exchange_running = False
        self.review_exchange_queries: list[ReviewExchangeRunningQuery] = []
        self.check_dirty_policy_calls: list[Path] = []

    def read_completion_record(
        self, worktree_path: Path, completion_path: str | None = None
    ) -> CompletionRecord | None:
        return self.completion_record

    def read_completion_record_result(
        self, worktree_path: Path, completion_path: str | None = None
    ) -> CompletionRecordLoadResult:
        if self.completion_load_result is not None:
            return self.completion_load_result
        path = worktree_path / (completion_path or ".issue-orchestrator/completion.json")
        if self.completion_record is None:
            return CompletionRecordLoadResult(
                path=path,
                failure=CompletionRecordLoadFailure.MISSING,
                error="Completion record not found",
            )
        return CompletionRecordLoadResult(
            path=path,
            record=self.completion_record,
            exists=True,
        )

    def process(
        self,
        worktree_path: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: SessionRunAssets,
        pr_number: int | None = None,
        completion_path: str | None = None,
    ):
        self.process_calls.append(
            {
                "worktree_path": worktree_path,
                "issue_number": issue_number,
                "issue_title": issue_title,
                "pr_number": pr_number,
                "completion_path": completion_path,
                "run_assets": run_assets,
            }
        )
        return self.process_result

    def validate_worktree_state(
        self, worktree_path: Path, record: CompletionRecord
    ) -> WorktreeValidationResult:
        return self._worktree_state_result()

    def check_dirty_policy(self, worktree_path: Path) -> WorktreeValidationResult:
        self.check_dirty_policy_calls.append(worktree_path)
        if self.dirty_policy_results:
            return self.dirty_policy_results.pop(0)
        return self._worktree_state_result()

    def is_review_exchange_running_for_completion(
        self,
        query: ReviewExchangeRunningQuery,
    ) -> bool:
        self.review_exchange_queries.append(query)
        return self.review_exchange_running and query.requires_review_exchange

    def completion_finalization_plan(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        run_id: str,
        outcome: CompletionOutcome,
        requested_actions: tuple[RequestedAction, ...],
        runtime_state: CompletionRuntimeState,
        validation_preflight_configured: bool,
    ):
        command = CompletionFinalizationCommand(
            issue_number=issue_number,
            session_name=session_name,
            outcome=outcome,
            requested_actions=requested_actions,
            runtime_state=runtime_state,
            review_exchange_running=self.is_review_exchange_running_for_completion(
                ReviewExchangeRunningQuery(
                    issue_number=issue_number,
                    session_name=session_name,
                    requested_actions=requested_actions,
                    run_id=run_id,
                )
            ),
            validation_preflight_configured=validation_preflight_configured,
        )
        return decide_completion_finalization(command)

    def deferred_review_exchange_result(self) -> ProcessingResult:
        return ProcessingResult.for_review_exchange_deferred()

    def _worktree_state_result(self) -> WorktreeValidationResult:
        if self.worktree_state_valid:
            return WorktreeValidationResult.pass_()
        return WorktreeValidationResult.fail(
            WorktreeValidationFailure.DIRTY_POLICY,
            self.worktree_state_reason,
        )


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class TestSessionControllerRunning:
    """Tests for RUNNING observation."""

    def test_running_session_returns_running_status(self):
        """A running session should stay running."""
        processor = MockCompletionProcessor()
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.running(runtime_minutes=5.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.RUNNING
        assert not decision.completion_processed


class TestSessionControllerTerminated:
    """Tests for TERMINATED observation (session exited)."""

    def test_provider_success_is_returned_as_decision_effect(
        self,
        tmp_path: Path,
    ) -> None:
        """Provider success effects are applied by the tick thread, not decide_outcome."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(CompletionOutcome.COMPLETED)
        session_output = FileSystemSessionOutput()
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=session_output,
            working_copy=StubWorkingCopy(),
        )
        session_name = "issue-123"
        worktree = tmp_path / "worktree"
        run_assets = make_session_run_assets(worktree, session_name, session_output)
        write_provider_status(
            run_assets.run_dir,
            ProviderStatus(
                provider="codex",
                error_type=None,
                attempts=1,
                succeeded=True,
                exit_code=0,
                timed_out=False,
                last_error_summary=None,
                last_attempt_at=now_iso(),
            ),
        )

        decision = controller.decide_outcome(
            SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree,
            123,
            "Test Issue",
            session_name,
            session_run_assets=run_assets,
        )

        assert decision.status == SessionStatus.COMPLETED
        assert decision.provider_success == "codex"

    def test_provider_transient_failure_is_returned_as_decision_effect(
        self,
        tmp_path: Path,
    ) -> None:
        """Provider failure effects are applied by the tick thread, not decide_outcome."""
        processor = MockCompletionProcessor()
        processor.completion_record = None
        session_output = FileSystemSessionOutput()
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=session_output,
            working_copy=StubWorkingCopy(),
            provider_blocked_label="blocked:provider-unavailable",
        )
        session_name = "issue-123"
        worktree = tmp_path / "worktree"
        run_assets = make_session_run_assets(worktree, session_name, session_output)
        write_provider_status(
            run_assets.run_dir,
            ProviderStatus(
                provider="codex",
                error_type=ProviderErrorType.TRANSIENT,
                attempts=3,
                succeeded=False,
                exit_code=1,
                timed_out=False,
                last_error_summary="provider overloaded",
                last_attempt_at=now_iso(),
            ),
        )

        decision = controller.decide_outcome(
            SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree,
            123,
            "Test Issue",
            session_name,
            session_run_assets=run_assets,
        )

        assert decision.status == SessionStatus.BLOCKED
        assert decision.provider_transient_failure is not None
        assert decision.provider_transient_failure.provider == "codex"
        assert decision.provider_transient_failure.error_summary == "provider overloaded"
        assert decision.provider_transient_failure.attempts == 3

    def test_terminated_without_completion_record_is_failed(self, tmp_path: Path):
        """Session that exits without completion.json = FAILED."""
        processor = MockCompletionProcessor()
        processor.completion_record = None  # No completion record
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.FAILED
        assert not decision.completion_processed
        assert "without completion record" in decision.reason

    def test_terminated_with_invalid_completion_record_surfaces_parse_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """Present-but-invalid completion JSON is not reported as missing."""
        worktree = tmp_path / "worktree"
        completion_rel = ".issue-orchestrator/completion-agent_backend.json"
        completion_path = worktree / completion_rel
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text('{"outcome": "completed"}\n', encoding="utf-8")

        processor = MockCompletionProcessor()
        processor.completion_load_result = CompletionRecordLoadResult(
            path=completion_path,
            failure=CompletionRecordLoadFailure.INVALID_SCHEMA,
            error="follow_up_issues exceeds maximum count (6 > 5)",
            exists=True,
            size=completion_path.stat().st_size,
        )
        event_sink = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=event_sink,
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            completion_path=completion_rel,
        )

        assert decision.status == SessionStatus.FAILED
        assert decision.completion_processed is False
        assert processor.process_calls == []
        assert decision.reason.startswith("Completion record rejected")
        assert decision.completion_detail is not None
        assert decision.completion_detail["failure_kind"] == "invalid_completion_record"
        assert decision.completion_detail["completion_load_failure"] == "invalid_schema"
        assert "follow_up_issues exceeds" in decision.completion_detail["completion_parse_error"]
        assert decision.diagnostic_path is not None
        diagnostic_path = Path(decision.diagnostic_path)
        assert diagnostic_path.exists()
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
        assert diagnostic["kind"] == "invalid-completion-record"

        invalid_events = [
            event
            for event in event_sink.events
            if event.name == EventName.SESSION_INVALID_COMPLETION_RECORD
        ]
        assert len(invalid_events) == 1
        payload = invalid_events[0].data
        assert payload["completion_load_failure"] == "invalid_schema"
        assert "follow_up_issues exceeds" in payload["completion_parse_error"]
        assert payload["completion_path_absolute"] == str(completion_path.resolve())
        assert payload["diagnostic_path"] == str(diagnostic_path)

    def test_terminated_with_completed_record_is_completed(self):
        """Session that exits with completed outcome = COMPLETED."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.COMPLETED
        assert decision.completion_processed
        assert not decision.recovered_from_timeout

    def test_pre_publish_validation_failure_becomes_validation_failed(self, tmp_path: Path):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        processor.process_result.success = False
        processor.process_result.failure_kind = "validation_failed"
        processor.process_result.message = "Validation failed: ERROR: Test-skipping patterns detected"
        event_sink = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=event_sink,
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)
        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.VALIDATION_FAILED
        assert decision.completion_processed
        validation_events = [
            event for event in event_sink.events
            if event.name == EventName.SESSION_VALIDATION_FAILED
        ]
        assert len(validation_events) == 1

    def test_pre_publish_validation_failure_rerouted_to_review_exchange_keeps_running(
        self, tmp_path: Path
    ):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        processor.process_result.success = True
        processor.process_result.message = (
            "Validation failed after review approval; review exchange resumed to rework the failure"
        )
        processor.process_result.review_exchange_deferred = True
        processor.process_result.validation_failed_rerouted = True
        event_sink = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=event_sink,
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)
        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.RUNNING
        assert decision.completion_processed is False
        validation_events = [
            event for event in event_sink.events
            if event.name == EventName.SESSION_VALIDATION_FAILED
        ]
        assert len(validation_events) == 1

    def test_deferred_review_exchange_keeps_session_running(self):
        """When process() defers for async review exchange, session stays active."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        # Background exchange is running — the processor reports deferred so the
        # session controller must not emit processing_completed, must not advance
        # status, and must leave completion_processed=False so the next tick
        # re-enters the pipeline.
        processor.process_result.review_exchange_deferred = True
        event_sink = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=event_sink,
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)
        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.RUNNING
        assert decision.completion_processed is False
        assert decision.processing_result is processor.process_result
        # No processing.completed event should have fired — the work isn't done.
        emitted = {getattr(e, "event", None) for e in event_sink.events}
        processing_completed_events = {
            name for name in emitted if name and "processing_completed" in str(name)
        }
        assert not processing_completed_events, (
            f"unexpected processing_completed emission: {emitted}"
        )

    def test_timed_out_deferred_review_exchange_returns_timeout(self):
        """A timed-out deferred exchange must not keep the session running."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        processor.process_result.review_exchange_deferred = True
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=121.0,
            timeout_minutes=120,
            session_exists=True,
        )
        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.TIMED_OUT
        assert decision.completion_processed is True
        assert decision.recovered_from_timeout is True
        assert decision.processing_result is not processor.process_result
        assert processor.process_result.review_exchange_deferred is True
        assert processor.process_result.review_exchange_halted is False
        assert processor.process_result.errors is None
        assert decision.processing_result.review_exchange_deferred is False
        assert decision.processing_result.review_exchange_halted is True
        assert decision.processing_result.errors == [
            "review_exchange: Session timed out while review exchange was still running"
        ]

    def test_timed_out_deferred_review_exchange_cancels_runtime(self):
        """A terminal timeout must tear down hidden review-exchange work."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        processor.process_result.review_exchange_deferred = True
        cancellations: list[tuple[int, str]] = []

        class _Cancellation:
            cancelled_job_ids = ("review-exchange:123:coding-1",)

        def cancel(issue_number: int, reason: str) -> _Cancellation:
            cancellations.append((issue_number, reason))
            return _Cancellation()

        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
            review_exchange_canceller=cancel,
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=121.0,
            timeout_minutes=120,
            session_exists=True,
        )
        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.TIMED_OUT
        assert decision.processing_result is not None
        assert decision.processing_result.review_exchange_halted is True
        assert cancellations == [(123, "session-timeout")]

    def test_timed_out_running_review_exchange_halts_before_processing(self):
        """The terminal finalization decision is handled explicitly."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        processor.review_exchange_running = True
        cancellations: list[tuple[int, str]] = []

        class _Cancellation:
            cancelled_job_ids = ("review-exchange:123:issue-123",)

        def cancel(issue_number: int, reason: str) -> _Cancellation:
            cancellations.append((issue_number, reason))
            return _Cancellation()

        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
            review_exchange_canceller=cancel,
        )

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.timed_out(
                runtime_minutes=121.0,
                timeout_minutes=120,
                session_exists=True,
            ),
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.TIMED_OUT
        assert decision.completion_processed is True
        assert decision.processing_result is not None
        assert decision.processing_result.review_exchange_deferred is False
        assert decision.processing_result.review_exchange_halted is True
        assert decision.processing_result.errors == [
            "review_exchange: visible session timed out while review exchange is running"
        ]
        assert processor.process_calls == []
        assert cancellations == [(123, "session-timeout")]

    def test_terminated_with_blocked_record_is_blocked(self):
        """Session that exits with blocked outcome = BLOCKED."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.BLOCKED,
            summary="Blocked by dependency",
            blocked_reason="Waiting for API",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.BLOCKED
        assert decision.completion_processed
        assert decision.blocked_reason == "Waiting for API"

    def test_terminated_with_needs_human_record_is_needs_human(self):
        """Session that exits with needs_human outcome = NEEDS_HUMAN."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.NEEDS_HUMAN,
            summary="Need clarification",
            question="What API should I use?",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.NEEDS_HUMAN
        assert decision.completion_processed


class TestSessionControllerTimeout:
    """Tests for TIMED_OUT observation - the key recovery case."""

    def test_timeout_without_completion_record_is_timed_out(self, tmp_path: Path):
        """Session that times out without completion.json = TIMED_OUT."""
        processor = MockCompletionProcessor()
        processor.completion_record = None  # No completion record
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.TIMED_OUT
        assert not decision.completion_processed
        assert not decision.recovered_from_timeout

    def test_timeout_without_completion_writes_diagnostic(self, tmp_path: Path):
        """Missing completion writes a no-completion diagnostic artifact."""
        processor = MockCompletionProcessor()
        processor.completion_record = None
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )
        completion_rel_path = (
            ".issue-orchestrator/sessions/coding-1/completion-agent_backend.json"
        )
        run = make_session_run_assets(tmp_path, "issue-123", controller.session_output)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            completion_path=completion_rel_path,
            session_run_assets=run,
        )

        diagnostic_files = list(run.run_dir.glob("no-completion-*.json"))
        assert len(diagnostic_files) == 1
        diagnostic = json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
        assert diagnostic["kind"] == "no-completion-record"
        assert diagnostic["requested_completion_path"] == completion_rel_path
        assert diagnostic["requested_completion_exists"] is False
        assert diagnostic["observation"] == "timed_out"
        assert diagnostic["agent_done_marker_exists"] is False
        assert diagnostic["nearby_completion_candidates"] == []
        assert controller.session_output.find_run_dir(tmp_path, "coding-1") is None
        assert decision.status == SessionStatus.TIMED_OUT

    def test_timeout_without_completion_uses_recorded_run_dir(self, tmp_path: Path):
        """A launched session's run_dir is authoritative for diagnostics."""
        processor = MockCompletionProcessor()
        processor.completion_record = None
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "launch-recorded", issue_number=123)
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=session_output,
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )
        completion_rel_path = (
            ".issue-orchestrator/sessions/coding-1/completion-agent_backend.json"
        )

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            completion_path=completion_rel_path,
            session_run_assets=run,
        )

        diagnostic_files = list(run.run_dir.glob("no-completion-*.json"))
        assert len(diagnostic_files) == 1
        assert session_output.find_run_dir(tmp_path, "coding-1") is None
        assert decision.status == SessionStatus.TIMED_OUT

    def test_timeout_without_completion_records_marker_and_nearby_candidates(
        self, tmp_path: Path
    ):
        """No-completion diagnostics should capture marker presence and alternate completion files."""
        processor = MockCompletionProcessor()
        processor.completion_record = None
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        (tmp_path / ".agent-done-marker").write_text(
            "agent-done completed called at 2026-03-17T00:00:00\n",
            encoding="utf-8",
        )
        alt_completion = (
            tmp_path
            / ".issue-orchestrator"
            / "sessions"
            / "other-run"
            / "completion-agent_backend.json"
        )
        alt_completion.parent.mkdir(parents=True, exist_ok=True)
        alt_completion.write_text('{"outcome":"completed"}', encoding="utf-8")

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )
        completion_rel_path = (
            ".issue-orchestrator/sessions/coding-1/completion-agent_backend.json"
        )
        run = make_session_run_assets(tmp_path, "issue-123", controller.session_output)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            completion_path=completion_rel_path,
            session_run_assets=run,
        )

        diagnostic_files = list(run.run_dir.glob("no-completion-*.json"))
        diagnostic = json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
        assert diagnostic["agent_done_marker_exists"] is True
        assert "agent-done completed" in diagnostic["agent_done_marker_preview"]
        nearby = diagnostic["nearby_completion_candidates"]
        assert len(nearby) == 1
        assert nearby[0]["path"] == str(alt_completion.resolve())
        assert nearby[0]["under_run_dir"] is False
        assert decision.status == SessionStatus.TIMED_OUT

    def test_timeout_with_completed_record_is_recovered(self):
        """Session that times out WITH completion.json = COMPLETED (recovered)."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Key assertion: even though timeout occurred, work was recovered
        assert decision.status == SessionStatus.COMPLETED
        assert decision.completion_processed
        assert decision.recovered_from_timeout

    def test_timeout_with_blocked_record_is_blocked_recovered(self):
        """Session that times out with blocked outcome = BLOCKED (recovered)."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.BLOCKED,
            summary="Blocked",
            blocked_reason="External dependency",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.BLOCKED
        assert decision.completion_processed
        assert decision.recovered_from_timeout
        assert decision.blocked_reason == "External dependency"


class TestSessionControllerReviewOutcomes:
    """Tests for review session outcomes."""

    def test_review_approved_is_completed(self):
        """Review approved outcome maps to COMPLETED status."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.REVIEW_APPROVED,
            summary="LGTM",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=5.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="review-456",
        )

        assert decision.status == SessionStatus.COMPLETED

    def test_review_changes_requested_is_completed(self):
        """Review changes_requested outcome maps to COMPLETED status."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            summary="Needs work",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=5.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="review-456",
        )

        # Review session completed its job (even if changes requested)
        assert decision.status == SessionStatus.COMPLETED


class MockCommandRunner:
    """Mock command runner for testing validation."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.run_calls: list[dict] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        """Record the call and return configured result."""
        self.run_calls.append(
            {
                "command": command,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "shell": shell,
            }
        )
        # Return a result-like object
        from types import SimpleNamespace

        return SimpleNamespace(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


class MockWorkingCopy:
    """Mock WorkingCopy for testing validation caching."""

    def __init__(self, head_sha: str = "abc1234567890"):
        self.head_sha = head_sha
        self.get_head_sha_calls: list[Path] = []

    def get_head_sha(self, worktree: Path) -> str | None:
        self.get_head_sha_calls.append(worktree)
        return self.head_sha

    def get_current_branch(self, worktree: Path) -> str | None:
        return "test-branch"


class TestSessionControllerValidationCaching:
    """Tests for validation caching via PublishGate."""

    def test_review_only_session_skips_code_validation_gate(self, tmp_path: Path):
        """A retrospective review with a failing validation command must not retry.

        Regression for #6426: a review-only session makes no commits and
        publishes nothing, so feeding its (often transient) validation failure
        into the coder retry loop relaunched the work as ``TaskKind.CODE``, which
        then tried to open a PR on an empty branch -> publish-failed. The code
        validation gate must be skipped entirely for review-only task kinds.
        """
        from issue_orchestrator.domain.session_key import TaskKind

        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.REVIEW_APPROVED,
            summary="Existing implementation looks good",
            requested_actions=[],
        )

        # A failing validation command would normally force a retry.
        command_runner = MockCommandRunner(returncode=1, stderr="network timeout")
        events = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="./scripts/validate-quick.sh",
            validation_timeout_seconds=60,
            max_validation_retries=3,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=361,
            issue_title="Review Existing Implementation #361",
            session_name="retrospective-review-361",
            task_kind=TaskKind.RETROSPECTIVE_REVIEW,
        )

        # The review completes on its read-only path: no retry, no gate run.
        assert decision.status == SessionStatus.COMPLETED
        assert decision.validation_passed is None
        assert command_runner.run_calls == []
        retry_events = [
            event
            for event in events.events
            if event.event_type == EventName.SESSION_VALIDATION_RETRY_NEEDED
        ]
        assert retry_events == []

    def test_code_session_still_runs_validation_gate(self, tmp_path: Path):
        """Coding sessions keep running the validation gate (skip is review-only).

        Companion to ``test_review_only_session_skips_code_validation_gate`` to
        prove the skip is scoped to review-only tasks, not a blanket change.
        """
        from issue_orchestrator.domain.session_key import TaskKind

        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=1, stderr="real failure")
        controller = SessionController(
            completion_processor=processor,
            events=RecordingEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            max_validation_retries=3,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            task_kind=TaskKind.CODE,
        )

        assert decision.status == SessionStatus.NEEDS_VALIDATION_RETRY
        assert decision.validation_passed is False
        assert len(command_runner.run_calls) == 1

    def test_dirty_preflight_defers_while_review_exchange_running(
        self, tmp_path: Path
    ):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        processor.review_exchange_running = True
        processor.worktree_state_valid = False
        processor.worktree_state_reason = (
            "Working tree is dirty; commit/add/stash before pushing. "
            "Dirty files: scripts/dev.sh."
        )

        command_runner = MockCommandRunner(returncode=0)
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            max_validation_retries=1,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.RUNNING
        assert decision.completion_processed is False
        assert decision.processing_result is not None
        assert decision.processing_result.review_exchange_deferred is True
        assert command_runner.run_calls == []
        assert processor.process_calls == []
        assert processor.check_dirty_policy_calls == []

    def test_dirty_preflight_returns_validation_retry_without_running_command(
        self, tmp_path: Path
    ):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        processor.dirty_policy_results = [
            WorktreeValidationResult.dirty_policy_failure(
                "Working tree is dirty; commit/add/stash before pushing. "
                "Dirty files: scripts/dev.sh.",
                blocking_paths=("scripts/dev.sh",),
            )
        ]

        command_runner = MockCommandRunner(returncode=0)
        events = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            max_validation_retries=1,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.NEEDS_VALIDATION_RETRY
        assert decision.validation_passed is False
        assert "before running command" in decision.validation_error
        assert "scripts/dev.sh" in decision.validation_error
        assert command_runner.run_calls == []
        assert processor.process_calls == []

        retry_prompt = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "issue-123"
            / "retry-prompt.md"
        )
        assert retry_prompt.exists()
        retry_prompt_content = retry_prompt.read_text()
        assert "No validation command ran" in retry_prompt_content
        assert "Your changes broke validation" not in retry_prompt_content
        assert "prepush-check --dirty-only -v" in retry_prompt_content
        assert "scripts/dev.sh" in retry_prompt_content
        retry_events = [
            event
            for event in events.events
            if event.event_type == EventName.SESSION_VALIDATION_RETRY_NEEDED
        ]
        assert len(retry_events) == 1
        assert retry_events[0].data["validation_failure_kind"] == (
            "dirty_before_validation"
        )
        assert retry_events[0].data["dirty_files"] == ["scripts/dev.sh"]

    def test_dirty_preflight_exhausts_without_running_command(self, tmp_path: Path):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        processor.dirty_policy_results = [
            WorktreeValidationResult.dirty_policy_failure(
                "Working tree is dirty; commit/add/stash before pushing. "
                "Dirty files: scripts/dev.sh.",
                blocking_paths=("scripts/dev.sh",),
            )
        ]

        command_runner = MockCommandRunner(returncode=0)
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            max_validation_retries=0,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.VALIDATION_FAILED
        assert decision.validation_passed is False
        assert command_runner.run_calls == []
        assert processor.process_calls == []

    def test_dirty_postflight_returns_validation_retry_after_command(
        self, tmp_path: Path
    ):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        processor.dirty_policy_results = [
            WorktreeValidationResult.pass_(),
            WorktreeValidationResult.dirty_policy_failure(
                "Working tree is dirty; commit/add/stash before pushing. "
                "Dirty files: generated.txt.",
                blocking_paths=("generated.txt",),
            ),
        ]

        command_runner = MockCommandRunner(returncode=0)
        events = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            max_validation_retries=1,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.NEEDS_VALIDATION_RETRY
        assert decision.validation_passed is False
        assert "Validation command passed" in decision.validation_error
        assert "generated.txt" in decision.validation_error
        assert len(command_runner.run_calls) == 1
        assert len(processor.process_calls) == 1
        retry_events = [
            event
            for event in events.events
            if event.event_type == EventName.SESSION_VALIDATION_RETRY_NEEDED
        ]
        assert len(retry_events) == 1

    def test_dirty_postflight_exhausts_after_command(self, tmp_path: Path):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        processor.dirty_policy_results = [
            WorktreeValidationResult.pass_(),
            WorktreeValidationResult.dirty_policy_failure(
                "Working tree is dirty; commit/add/stash before pushing. "
                "Dirty files: generated.txt.",
                blocking_paths=("generated.txt",),
            ),
        ]

        command_runner = MockCommandRunner(returncode=0)
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            max_validation_retries=0,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.VALIDATION_FAILED
        assert decision.validation_passed is False
        assert "generated.txt" in decision.validation_error
        assert len(command_runner.run_calls) == 1
        assert len(processor.process_calls) == 1

    def test_validation_runs_with_sha_logged(self, tmp_path):
        """Validation runs and logs SHA on cache miss."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=0)
        working_copy = MockWorkingCopy(head_sha="deadbeef1234567890")

        events = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
        )

        # Create worktree with git repo so PublishGate can read SHA
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Validation should pass
        assert decision.status == SessionStatus.COMPLETED
        assert decision.validation_passed is True

        # Command should have been run
        assert len(command_runner.run_calls) == 1
        assert command_runner.run_calls[0]["command"] == "make test"

        # SHA should have been fetched
        assert len(working_copy.get_head_sha_calls) >= 1
        validation_event = next(
            event
            for event in events.events
            if event.event_type == EventName.SESSION_VALIDATION_PASSED
        )
        artifacts = validation_event.data["artifacts"]
        assert artifacts == [
            {
                "type": "validation",
                "label": "Validation Record",
                "value": str(
                    Path(validation_event.data["run_dir"]) / "validation-record.json"
                ),
            }
        ]
        assert Path(artifacts[0]["value"]).exists()

    def test_validation_records_junit_evidence_in_run_manifest(self, tmp_path: Path):
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        session_output = FileSystemSessionOutput()
        from issue_orchestrator.execution.run_evidence import RunEvidenceRecorder

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        junit_path = worktree / "reports" / "junit.xml"

        class JUnitWritingCommandRunner(MockCommandRunner):
            def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
                junit_path.parent.mkdir()
                junit_path.write_text(
                    """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="validation" tests="1">
  <testcase classname="tests.unit.test_smoke" name="test_smoke" time="0.01" />
</testsuite>
""",
                    encoding="utf-8",
                )
                return super().run(
                    command,
                    cwd=cwd,
                    env=env,
                    timeout_seconds=timeout_seconds,
                    shell=shell,
                )

        controller = SessionController(
            completion_processor=processor,
            events=RecordingEventSink(),
            session_output=session_output,
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=JUnitWritingCommandRunner(returncode=0),
            validation_cmd="make test",
            validation_timeout_seconds=60,
            validation_junit_xml_paths=("reports/*.xml",),
            validation_evidence_recorder=RunEvidenceRecorder(session_output),
        )

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.COMPLETED
        run_dir = session_output.find_run_dir(worktree, session_name="issue-123")
        assert run_dir is not None
        manifest = session_output.read_manifest(run_dir)
        assert manifest is not None
        assert any(
            artifact.get("kind") == "junit_xml"
            and artifact.get("path") == str(junit_path.resolve())
            for artifact in manifest["artifacts"].values()
        )

    def test_cached_validation_pass_materializes_record_for_pass_event(self, tmp_path):
        """Cached validation passes still emit concrete run-scoped evidence."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=0)
        events = RecordingEventSink()
        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        for issue_number, session_name in ((123, "issue-123"), (124, "issue-124")):
            decision = decide_with_run_assets(
            controller,
                observation=SessionObservationResult.terminated(runtime_minutes=10.0),
                worktree_path=worktree,
                issue_number=issue_number,
                issue_title="Test Issue",
                session_name=session_name,
            )
            assert decision.status == SessionStatus.COMPLETED
            assert decision.validation_passed is True

        assert len(command_runner.run_calls) == 1
        validation_events = [
            event
            for event in events.events
            if event.event_type == EventName.SESSION_VALIDATION_PASSED
        ]
        assert len(validation_events) == 2
        cached_event = validation_events[-1]
        cached_record_path = (
            Path(cached_event.data["run_dir"]) / "validation-record.json"
        )
        assert cached_event.data["artifacts"] == [
            {
                "type": "validation",
                "label": "Validation Record",
                "value": str(cached_record_path),
            }
        ]
        assert cached_record_path.exists()

    def test_attempt_cache_reuses_validation_for_same_issue(self, tmp_path: Path):
        """Attempt-scoped cache reuses validation within the same issue and SHA."""
        from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
        from issue_orchestrator.domain.attempt import AttemptKey
        from issue_orchestrator.domain.issue_key import GitHubIssueKey

        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        full_sha = "a" * 40
        command_runner = MockCommandRunner(returncode=0)
        attempt_store = SidecarAttemptStore(tmp_path / "attempt-store")

        class AttemptKeyFactory:
            def for_validation_attempt(
                self,
                *,
                issue_key,
                head_sha: str,
            ):
                return AttemptKey(issue_key, head_sha)

        controller = SessionController(
            completion_processor=processor,
            events=RecordingEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha=full_sha),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            attempt_store=attempt_store,
            validation_attempt_key_factory=AttemptKeyFactory(),
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        for session_name in ("issue-123-a", "issue-123-b"):
            decision = decide_with_run_assets(
            controller,
                observation=SessionObservationResult.terminated(runtime_minutes=10.0),
                worktree_path=worktree,
                issue_number=123,
                issue_title="Test Issue",
                session_name=session_name,
                issue_key=GitHubIssueKey(repo="owner/repo", external_id="123"),
            )
            assert decision.status == SessionStatus.COMPLETED
            assert decision.validation_passed is True

        assert len(command_runner.run_calls) == 1

    def test_attempt_cache_does_not_share_validation_across_issues(
        self, tmp_path: Path
    ):
        """Attempt-scoped cache blocks SHA-only reuse across different issues."""
        from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
        from issue_orchestrator.domain.attempt import AttemptKey
        from issue_orchestrator.domain.issue_key import GitHubIssueKey

        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        full_sha = "b" * 40
        command_runner = MockCommandRunner(returncode=0)
        attempt_store = SidecarAttemptStore(tmp_path / "attempt-store")

        class AttemptKeyFactory:
            def for_validation_attempt(
                self,
                *,
                issue_key,
                head_sha: str,
            ):
                return AttemptKey(issue_key, head_sha)

        controller = SessionController(
            completion_processor=processor,
            events=RecordingEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha=full_sha),
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
            attempt_store=attempt_store,
            validation_attempt_key_factory=AttemptKeyFactory(),
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        for issue_number in (123, 124):
            decision = decide_with_run_assets(
            controller,
                observation=SessionObservationResult.terminated(runtime_minutes=10.0),
                worktree_path=worktree,
                issue_number=issue_number,
                issue_title="Test Issue",
                session_name=f"issue-{issue_number}",
                issue_key=GitHubIssueKey(
                    repo="owner/repo", external_id=str(issue_number)
                ),
            )
            assert decision.status == SessionStatus.COMPLETED
            assert decision.validation_passed is True

        assert len(command_runner.run_calls) == 2

    def test_attempt_cache_requires_issue_key_when_store_is_wired(
        self, tmp_path: Path
    ):
        """Attempt cache wiring must not silently fall back to SHA-only caching."""
        from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
        from issue_orchestrator.domain.attempt import AttemptKey

        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        class AttemptKeyFactory:
            def for_validation_attempt(
                self,
                *,
                issue_key,
                head_sha: str,
            ):
                return AttemptKey(issue_key, head_sha)

        controller = SessionController(
            completion_processor=processor,
            events=RecordingEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="c" * 40),
            command_runner=MockCommandRunner(returncode=0),
            validation_cmd="make test",
            validation_timeout_seconds=60,
            attempt_store=SidecarAttemptStore(tmp_path / "attempt-store"),
            validation_attempt_key_factory=AttemptKeyFactory(),
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with pytest.raises(RuntimeError, match="stable IssueKey"):
            decide_with_run_assets(
            controller,
                observation=SessionObservationResult.terminated(runtime_minutes=10.0),
                worktree_path=worktree,
                issue_number=123,
                issue_title="Test Issue",
                session_name="issue-123",
            )

    def test_validation_pass_event_requires_validation_record(
        self, tmp_path, monkeypatch
    ):
        """A passed validation event must not silently omit validation evidence."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        controller = SessionController(
            completion_processor=processor,
            validation_cmd="make test",
            command_runner=MockCommandRunner(returncode=0),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
            session_output=FileSystemSessionOutput(),
            events=NullEventSink(),
        )
        monkeypatch.setattr(
            controller,
            "_run_validation",
            lambda *args, **kwargs: (True, None, None),
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with pytest.raises(
            FileNotFoundError,
            match="validation-record.json missing for passed validation event",
        ):
            decide_with_run_assets(
            controller,
                observation=SessionObservationResult.terminated(runtime_minutes=10.0),
                worktree_path=worktree,
                issue_number=123,
                issue_title="Test Issue",
                session_name="issue-123",
            )

    def test_validation_failure_returns_error(self, tmp_path):
        """Validation failure returns correct status and error info."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=1, stderr="Tests failed!")
        working_copy = MockWorkingCopy(head_sha="deadbeef1234567890")

        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Validation failed
        assert decision.status == SessionStatus.VALIDATION_FAILED
        assert decision.validation_passed is False
        assert decision.validation_error is not None

    def test_validation_retry_event_includes_validation_reason(self, tmp_path: Path):
        """Retry events should carry the concrete validation failure reason."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        events = RecordingEventSink()
        command_runner = MockCommandRunner(
            returncode=1, stderr="dashboard tests failed"
        )
        working_copy = MockWorkingCopy(head_sha="deadbeef1234567890")

        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            validation_cmd="make test-web",
            validation_timeout_seconds=60,
            max_validation_retries=3,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.NEEDS_VALIDATION_RETRY
        retry_event = next(
            event
            for event in events.events
            if event.event_type == EventName.SESSION_VALIDATION_RETRY_NEEDED
        )
        assert (
            retry_event.data["validation_reason"]
            == "Validation failed for deadbeef (exit_code=1)"
        )
        assert retry_event.data["validation_error_summary"] == "dashboard tests failed"

    def test_no_validation_when_no_command_configured(self):
        """No validation runs when validation_cmd is not configured."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=0)
        working_copy = MockWorkingCopy()

        # No validation_cmd
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            # validation_cmd not set
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Should complete without validation
        assert decision.status == SessionStatus.COMPLETED
        assert decision.validation_passed is None  # Not run

    def test_no_validation_command_dirty_worktree_uses_processor_backstop(
        self, tmp_path: Path
    ):
        """Dirty publish preconditions still fail without controller validation."""
        worktree = tmp_path / "worktree"
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )
        (completion_dir / "completion.json").write_text(
            json.dumps(record.to_dict()) + "\n"
        )

        config = Config()
        config.validation.publish.dirty_check = "tracked"
        label_adapter = MagicMock()
        pr_adapter = MagicMock()
        git_adapter = MagicMock()
        git_adapter.get_current_branch.return_value = "issue-123"
        git_adapter.has_tracked_changes.return_value = True
        git_adapter.list_dirty_files.return_value = ["scripts/dev.sh"]

        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=label_adapter,
            pr_adapter=pr_adapter,
            git_adapter=git_adapter,
            event_bus=None,
            session_output=FileSystemSessionOutput(),
            label_config={"validation_failed": "validation-failed"},
            config=config,
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=MockWorkingCopy(head_sha="deadbeef1234567890"),
        )

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.VALIDATION_FAILED
        assert decision.validation_passed is None
        label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        pr_adapter.add_comment.assert_called_once()
        git_adapter.push.assert_not_called()
