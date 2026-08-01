"""Tests for the async/deferred review-exchange path.

The main tick must never block on the review-exchange subprocess. These
tests exercise the seam where :class:`CompletionReviewExchange` either
submits the exchange to a background runner (returning ``deferred=True``)
or short-circuits onto a cached on-disk summary (returning the outcome).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from issue_orchestrator.control.completion_review_exchange import (
    CompletionReviewExchange,
)
from issue_orchestrator.domain.review_exchange import (
    ReviewExchangeOutcome,
    ReviewExchangeResponse,
)
from issue_orchestrator.domain.review_exchange_run import (
    ReviewExchangeRun,
    ReviewExchangeRunAssets,
)
from issue_orchestrator.domain.review_exchange_summary import ReviewExchangeSummaryV1
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.ports.background_job import CompletedJob
from issue_orchestrator.ports.session_output import ReviewExchangeSummary, SessionOutput
from tests.callback_endpoint_helpers import ready_callback_endpoint


class _FakeReviewExchangeRunner:
    """Minimal :class:`ReviewExchangeRunner` stand-in for async-path tests.

    The async test suite cares about how ``CompletionReviewExchange``
    schedules work and emits events around the runner call, not about
    what the runner itself does. This fake returns a canned outcome
    that satisfies the type signature; tests that need to drive the
    runner explicitly (e.g. failure path) replace it with their own
    implementation per-test.
    """

    def run(self, **kwargs: Any) -> ReviewExchangeOutcome:
        exchange_run = kwargs["exchange_run"]
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="reviewer_ok",
            run_assets=exchange_run.assets,
            reviewer_response=None,
            summary=_summary(
                status="ok",
                reason="reviewer_ok",
                rounds=1,
            ),
        )

    def job_timeout_seconds(self, **_: Any) -> float | None:
        return 60.0


class _FakeJobRunner:
    """Records submitted jobs and lets tests control whether they are 'running'."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, Callable[[], None]]] = []
        self._running: set[str] = set()
        self._completed: list[CompletedJob] = []

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        if job_id in self._running:
            return False
        self._running.add(job_id)
        self.submitted.append((job_id, fn))
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self._running

    def drain_completed(self) -> list[CompletedJob]:
        done = self._completed
        self._completed = []
        return done

    def finish(self, job_id: str, error: BaseException | None = None) -> None:
        self._running.discard(job_id)
        self._completed.append(CompletedJob(job_id=job_id, error=error))


class _CapturingSupervisor:
    def __init__(self) -> None:
        self.submitted_timeout: float | None = None

    def tick(self) -> None:
        pass

    def take_failure(self, job_id: str) -> CompletedJob | None:  # noqa: ARG002
        return None

    def is_running(self, job_id: str) -> bool:  # noqa: ARG002
        return False

    def submit(
        self,
        job_id: str,  # noqa: ARG002
        fn: Callable[[], None],  # noqa: ARG002
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        self.submitted_timeout = timeout_seconds
        return True


@dataclass
class _CapturedSummary:
    summary: ReviewExchangeSummaryV1
    review_run: ReviewExchangeRun


class _FakeSessionOutput:
    """Minimal SessionOutput stand-in exposing only what the async path touches."""

    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree
        self._summary: _CapturedSummary | None = None
        self.no_completion_count = 0
        self.started_runs: list[ReviewExchangeRun] = []
        self._run_dir = worktree / ".sessions" / "exchange-run"
        (self._run_dir / "review-exchange").mkdir(parents=True, exist_ok=True)

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def find_run_dir(self, worktree: Path, session_name: str) -> Path | None:
        raise AssertionError("review exchange tests must use typed run DI")

    def start_review_exchange_run(
        self,
        worktree: Path,
        *,
        issue_number: int,
        parent_session_name: str,
        agent_label: str,
    ) -> ReviewExchangeRun:
        run = ReviewExchangeRun(
            session_name=f"review-exchange-{issue_number}-{len(self.started_runs) + 1}",
            run_id=f"exchange-run-{len(self.started_runs) + 1}",
            parent_session_name=parent_session_name,
            assets=ReviewExchangeRunAssets.from_run_dir(self._run_dir),
        )
        self.started_runs.append(run)
        return run

    def cached_review_run(
        self, parent_session_name: str = "coding-1"
    ) -> ReviewExchangeRun:
        return ReviewExchangeRun(
            session_name="review-exchange-230",
            run_id="exchange-run-cached",
            parent_session_name=parent_session_name,
            assets=ReviewExchangeRunAssets.from_run_dir(self._run_dir),
        )

    def store_review_exchange_summary(
        self,
        review_run: ReviewExchangeRun,
        summary: ReviewExchangeSummaryV1,
    ) -> None:
        self._summary = _CapturedSummary(
            summary=summary,
            review_run=review_run,
        )

    def load_review_exchange_summary(
        self,
        worktree: Path,
        session_name: str,
        *,
        not_before_started_at: str | None = None,
    ):
        if self._summary is None:
            return None
        if not_before_started_at == "future-boundary":
            return None

        return ReviewExchangeSummary(
            summary=self._summary.summary,
            run_assets=self._summary.review_run.assets,
        )

    def count_consecutive_review_exchange_no_completion(
        self,
        worktree: Path,  # noqa: ARG002
        session_name: str,  # noqa: ARG002
        *,
        not_before_started_at: str | None = None,  # noqa: ARG002
    ) -> int:
        return self.no_completion_count


def _make_config(tmp_path: Path, *, require_validation: bool = False) -> Config:
    cfg = Config(repo_root=tmp_path)
    config_path = tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("validation:\n  quick:\n    cmd: 'true'\n", encoding="utf-8")
    cfg.config_path = config_path
    cfg.review_exchange_mode = "via-local-loop"
    cfg.review_exchange_require_validation = require_validation
    cfg.code_review_agent = "agent:reviewer"
    cfg.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            command="claude --print",
            reviewer="agent:reviewer",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=tmp_path / "reviewer.md",
            command="claude --print",
        ),
    }
    return cfg


def _make_record(
    pr: bool = True,
    validation_record_path: Path | None = None,
) -> CompletionRecord:
    actions = [RequestedAction.CREATE_PR, RequestedAction.PUSH_BRANCH] if pr else []
    return CompletionRecord(
        session_id="coding-1",
        timestamp="2026-04-17T00:00:00Z",
        outcome=CompletionOutcome.COMPLETED,
        summary="done",
        implementation="",
        problems="",
        requested_actions=actions,
        validation_record_path=(
            str(validation_record_path) if validation_record_path else None
        ),
    )


def _write_validation_record(path: Path, *, head_sha: str, passed: bool = True) -> None:
    path.write_text(json.dumps({"passed": passed, "head_sha": head_sha}))


def _summary(
    *,
    status: str,
    reason: str,
    rounds: int,
    response_text: str | None = None,
    head_sha: str | None = None,
    validation_passed: bool | None = None,
) -> ReviewExchangeSummaryV1:
    payload: dict[str, object] = {
        "status": status,
        "reason": reason,
        "completed_rounds": rounds,
        "response_text": response_text,
        "timestamp": "2026-02-01T00:00:00+00:00",
    }
    if head_sha is not None:
        payload["head_sha"] = head_sha
    if validation_passed is not None:
        payload["validation_passed"] = validation_passed
    return ReviewExchangeSummaryV1.from_payload(payload)


def _store_cached_approval(
    session_output: _FakeSessionOutput,
    worktree: Path,
    validation_record_path: Path | None,
) -> None:
    review_run = session_output.cached_review_run()
    if validation_record_path is not None and validation_record_path.exists():
        review_run.assets.validation_record_path.write_text(
            validation_record_path.read_text()
        )
    elif review_run.assets.validation_record_path.exists():
        review_run.assets.validation_record_path.unlink()
    session_output.store_review_exchange_summary(
        review_run,
        _summary(
            status="ok",
            reason="reviewer_ok",
            rounds=1,
            response_text="Looks good.",
        ),
    )


def _store_cached_halt(
    session_output: _FakeSessionOutput,
    worktree: Path,
    validation_record_path: Path | None,
) -> None:
    review_run = session_output.cached_review_run()
    if validation_record_path is not None and validation_record_path.exists():
        review_run.assets.validation_record_path.write_text(
            validation_record_path.read_text()
        )
    elif review_run.assets.validation_record_path.exists():
        review_run.assets.validation_record_path.unlink()
    session_output.store_review_exchange_summary(
        review_run,
        _summary(
            status="stopped",
            reason="max_rounds_exceeded",
            rounds=3,
            response_text="Max rounds reached.",
        ),
    )


def _build(
    tmp_path: Path,
    job_runner: _FakeJobRunner,
    started_events: list[dict[str, Any]],
    outcome_events: list[dict[str, Any]],
    *,
    require_validation: bool = False,
) -> tuple[CompletionReviewExchange, _FakeSessionOutput]:
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )

    session_output = _FakeSessionOutput(tmp_path)

    def _record_review_started(**kwargs: Any) -> None:
        started_events.append(kwargs)

    def _on_outcome(**kwargs: Any) -> None:
        outcome_events.append(kwargs)

    # Tests driving the async path wrap the fake runner in a real supervisor
    # — matching the production contract. Nothing calls supervisor.tick in
    # these tests because the fake runner doesn't raise; when failure paths
    # need testing, tests call `supervisor.tick()` themselves.
    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path, require_validation=require_validation),
        session_output=cast(SessionOutput, session_output),
        emit_review_started=_record_review_started,
        emit_review_outcome=_on_outcome,
        review_exchange_runner=_FakeReviewExchangeRunner(),
        job_supervisor=BackgroundJobSupervisor(job_runner),
    )
    return review, session_output


def test_first_pass_submits_background_job_and_returns_deferred(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    review, _ = _build(tmp_path, job_runner, started, outcomes)
    called = []

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        called.append(kwargs)
        raise AssertionError("loop must not run synchronously when deferred")

    (
        _plan,
        mode,
        outcome,
        completed,
        halt,
        deferred,
    ) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR, RequestedAction.PUSH_BRANCH),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert mode == "via-local-loop"
    # The job was submitted but the caller's loop did NOT run on this thread.
    assert len(job_runner.submitted) == 1
    assert called == []
    # Job id is stable for the same (issue, session_name).
    assert job_runner.submitted[0][0] == "review-exchange:230:coding-1:coding-run-1"


def test_background_job_forwards_approval_gate_to_loop(tmp_path: Path) -> None:
    """The async closure must carry the producer's gate to the runner unchanged."""
    job_runner = _FakeJobRunner()
    review, _ = _build(tmp_path, job_runner, [], [])
    calls: list[dict[str, Any]] = []

    class _ApprovalGate:
        def rejection_reason(self) -> str | None:
            return None

    approval_gate = _ApprovalGate()

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        calls.append(kwargs)
        exchange_run = kwargs["exchange_run"]
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="reviewer_ok",
            run_assets=exchange_run.assets,
            summary=_summary(status="ok", reason="reviewer_ok", rounds=1),
        )

    (_, _, _, _, _, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
        approval_gate=approval_gate,
    )

    assert deferred is True
    assert calls == []

    _job_id, submitted_fn = job_runner.submitted[0]
    submitted_fn()

    assert len(calls) == 1
    assert calls[0]["approval_gate"] is approval_gate


def test_background_deadline_is_derived_from_runner_port(tmp_path: Path) -> None:
    class _TimeoutRunner(_FakeReviewExchangeRunner):
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def job_timeout_seconds(self, **kwargs: Any) -> float | None:
            self.calls.append(kwargs)
            return 123.0

    runner = _TimeoutRunner()
    supervisor = _CapturingSupervisor()
    cfg = _make_config(tmp_path)
    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=cfg,
        session_output=cast(SessionOutput, _FakeSessionOutput(tmp_path)),
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=runner,
        job_supervisor=supervisor,  # type: ignore[arg-type]
    )

    (_, _, _, _, _, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=lambda **_: pytest.fail("must defer"),
    )

    assert deferred is True
    assert supervisor.submitted_timeout == 123.0
    assert runner.calls
    assert runner.calls[0]["max_rounds"] == cfg.review_exchange_max_rounds


def test_second_pass_while_running_keeps_deferring(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    review, _ = _build(tmp_path, job_runner, [], [])

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must not run")

    # Tick N: submits.
    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    # Tick N+1: still running.
    (_, _, _, _, _, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )
    assert deferred is True
    # Exactly one submission — second tick reused the in-flight job.
    assert len(job_runner.submitted) == 1


def test_running_background_job_without_deadline_halts(tmp_path: Path) -> None:
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )

    class _NoDeadlineRunner(_FakeReviewExchangeRunner):
        def job_timeout_seconds(self, **_: Any) -> float | None:
            return None

    job_runner = _FakeJobRunner()
    supervisor = BackgroundJobSupervisor(job_runner)
    cancellations: list[tuple[int, str, tuple[str, ...]]] = []

    @dataclass(frozen=True)
    class _Cancellation:
        cancelled_job_ids: tuple[str, ...]

    def cancel(issue_number: int, reason: str) -> _Cancellation:
        cancelled = tuple(
            supervisor.cancel_matching(
                lambda job_id: job_id.startswith(f"review-exchange:{issue_number}:"),
                reason=reason,
            )
        )
        cancellations.append((issue_number, reason, cancelled))
        return _Cancellation(cancelled_job_ids=cancelled)

    errors: list[str] = []
    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, _FakeSessionOutput(tmp_path)),
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_NoDeadlineRunner(),
        job_supervisor=supervisor,
        review_exchange_canceller=cancel,
    )

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must not run synchronously")

    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is True
    assert completed is False
    assert outcome is None
    assert len(job_runner.submitted) == 1
    assert cancellations == [
        (
            230,
            "background-job-unbounded",
            ("review-exchange:230:coding-1:coding-run-1",),
        )
    ]
    assert supervisor.is_running("review-exchange:230:coding-1:coding-run-1") is False
    assert errors == [
        "review_exchange: background job is running without a supervisor "
        "deadline: job_id=review-exchange:230:coding-1:coding-run-1"
    ]


def test_within_deadline_for_completion_returns_false_for_unbounded_job(
    tmp_path: Path,
) -> None:
    """An unbounded BG job (timeout_seconds=None) must NOT report as
    within-deadline — otherwise a TIMED_OUT visible session would defer
    indefinitely through the finalization matrix instead of falling
    through to the existing unbounded-job halt path in
    ``run_review_exchange_if_needed``.

    Reproduces the Codex review finding on PR #6359: ``deadline_exceeded``
    is False when ``timeout_seconds`` is None, so the original
    ``return status.running and not status.deadline_exceeded`` was True
    for unbounded jobs and let the matrix keep deferring forever.
    """
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )
    from issue_orchestrator.domain.completion_finalization import (
        ReviewExchangeRunningQuery,
    )

    class _NoDeadlineRunner(_FakeReviewExchangeRunner):
        def job_timeout_seconds(self, **_: Any) -> float | None:
            return None

    job_runner = _FakeJobRunner()
    supervisor = BackgroundJobSupervisor(job_runner)
    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, _FakeSessionOutput(tmp_path)),
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_NoDeadlineRunner(),
        job_supervisor=supervisor,
    )

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must not run synchronously")

    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    query = ReviewExchangeRunningQuery(
        issue_number=230,
        session_name="coding-1",
        run_id="coding-run-1",
        requested_actions=(RequestedAction.CREATE_PR,),
    )
    # Sanity: the BG job IS running (just without a deadline).
    assert review.is_review_exchange_running_for_completion(query) is True
    # The within-deadline accessor must report False for the unbounded
    # case so the finalization matrix can hand off to the terminal-cancel
    # path that owns the unbounded-job halt.
    assert review.is_review_exchange_within_deadline_for_completion(query) is False


def test_within_deadline_for_completion_returns_true_for_bounded_running_job(
    tmp_path: Path,
) -> None:
    """The within-deadline accessor returns True when the BG job has a
    supervisor deadline and is still inside it — the case the PR #6359
    backstop is meant to protect from outer-session cancellation.
    """
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )
    from issue_orchestrator.domain.completion_finalization import (
        ReviewExchangeRunningQuery,
    )

    job_runner = _FakeJobRunner()
    supervisor = BackgroundJobSupervisor(job_runner)
    # The default _FakeReviewExchangeRunner reports a 60s deadline, so
    # the BG job is bounded and still inside it.
    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, _FakeSessionOutput(tmp_path)),
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_FakeReviewExchangeRunner(),
        job_supervisor=supervisor,
    )

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must not run synchronously")

    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=231,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    query = ReviewExchangeRunningQuery(
        issue_number=231,
        session_name="coding-1",
        run_id="coding-run-1",
        requested_actions=(RequestedAction.CREATE_PR,),
    )
    assert review.is_review_exchange_running_for_completion(query) is True
    assert review.is_review_exchange_within_deadline_for_completion(query) is True


def test_background_deadline_failure_cancels_runtime(tmp_path: Path) -> None:
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )

    class _ShortTimeoutRunner(_FakeReviewExchangeRunner):
        def job_timeout_seconds(self, **_: Any) -> float | None:
            return 1.0

    @dataclass(frozen=True)
    class _Cancellation:
        cancelled_job_ids: tuple[str, ...]

    job_runner = _FakeJobRunner()
    now = 1000.0
    supervisor = BackgroundJobSupervisor(job_runner, clock=lambda: now)
    cancellations: list[tuple[int, str, tuple[str, ...]]] = []

    def cancel(issue_number: int, reason: str) -> _Cancellation:
        cancelled = tuple(
            supervisor.cancel_matching(
                lambda job_id: job_id.startswith(f"review-exchange:{issue_number}:"),
                reason=reason,
            )
        )
        cancellations.append((issue_number, reason, cancelled))
        return _Cancellation(cancelled_job_ids=cancelled)

    errors: list[str] = []
    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, _FakeSessionOutput(tmp_path)),
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_ShortTimeoutRunner(),
        job_supervisor=supervisor,
        review_exchange_canceller=cancel,
    )

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must not run synchronously")

    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    now = 1002.0
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is True
    assert completed is False
    assert outcome is None
    assert cancellations == [
        (
            230,
            "background-job-timeout",
            ("review-exchange:230:coding-1:coding-run-1",),
        )
    ]
    assert supervisor.is_running("review-exchange:230:coding-1:coding-run-1") is False
    assert any("background job exceeded deadline" in error for error in errors)


def test_tick_after_completion_resolves_cached_outcome(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_runner = _FakeJobRunner()
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    review, session_output = _build(tmp_path, job_runner, started, outcomes)
    caplog.set_level(
        logging.INFO,
        logger="issue_orchestrator.control.completion_review_exchange",
    )

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        exchange_run = kwargs["exchange_run"]
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="reviewer_ok",
            run_assets=exchange_run.assets,
            reviewer_response=ReviewExchangeResponse(
                response_type="ok", getting_closer=True, response_text="Looks good."
            ),
            summary=_summary(
                status="ok",
                reason="reviewer_ok",
                rounds=1,
                response_text="Looks good.",
            ),
        )

    # Tick N — submit.
    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )
    # Execute the submitted callable inline to simulate the background thread
    # finishing and writing summary.json.
    job_id, submitted_fn = job_runner.submitted[0]
    submitted_fn()
    job_runner.finish(job_id)

    # Tick N+k — cached summary present, exchange resolves.
    actions_taken: list[str] = []
    (_, _mode, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None and outcome.status == "ok"
    assert "Review exchange passed (cached)" in actions_taken
    assert started, "emit_review_started must fire when the typed run is allocated"
    approval_messages = [
        record.getMessage()
        for record in caplog.records
        if "[REVIEW_EXCHANGE] approval accepted" in record.getMessage()
    ]
    assert any("issue=230" in message for message in approval_messages)
    assert any("cached=True" in message for message in approval_messages)
    assert any(
        "reviewer_response_text='Looks good.'" in message
        for message in approval_messages
    )


def test_cached_review_is_reused_when_validation_sha_matches(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_runner = _FakeJobRunner()
    started_events: list[dict[str, Any]] = []
    outcome_events: list[dict[str, Any]] = []
    review, session_output = _build(
        tmp_path,
        job_runner,
        started_events,
        outcome_events,
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha")
    _write_validation_record(current_validation, head_sha="same-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)
    caplog.set_level(
        logging.INFO,
        logger="issue_orchestrator.control.completion_review_exchange",
    )

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("matching cached approval should be reused")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None and outcome.status == "ok"
    assert outcome.summary is not None
    assert not any(key.startswith("_cache_") for key in outcome.summary.to_payload())
    assert outcome.cache_metadata is not None
    assert outcome.cache_metadata.to_event_fields() == {
        "review_cache_summary_path": str(
            session_output.run_dir / "review-exchange" / "summary.json"
        ),
        "review_cache_validation_record_path": str(
            session_output.run_dir / "validation-record.json"
        ),
        "review_cache_head_sha": "same-sha",
    }
    assert started_events[0]["review_cache_head_sha"] == "same-sha"
    assert outcome_events[0]["review_cache_head_sha"] == "same-sha"
    assert actions_taken == ["Review exchange passed (cached)"]
    assert job_runner.submitted == []
    approval_message = next(
        record.getMessage()
        for record in caplog.records
        if "[REVIEW_EXCHANGE] approval accepted" in record.getMessage()
    )
    assert "issue=230" in approval_message
    assert "session=coding-1" in approval_message
    assert "cached=True" in approval_message
    assert "head_sha=same-sha" in approval_message
    assert (
        f"summary_path={session_output.run_dir / 'review-exchange' / 'summary.json'}"
        in approval_message
    )
    assert "reviewer_response_text='Looks good.'" in approval_message


def test_cached_review_reuses_rework_head_when_completion_validation_is_stale(
    tmp_path: Path,
) -> None:
    """A review exchange can advance HEAD after the original coding-done record.

    The cached approval must be compared to the actual worktree HEAD, not the
    stale validation path embedded in the original completion record.
    """
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    stale_completion_validation = tmp_path / "original-validation.json"
    cached_rework_validation = tmp_path / "rework-validation.json"
    _write_validation_record(
        stale_completion_validation,
        head_sha="original-sha",
        passed=False,
    )
    _write_validation_record(cached_rework_validation, head_sha="rework-sha")
    _store_cached_approval(session_output, tmp_path, cached_rework_validation)

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=277,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=stale_completion_validation),
        current_head_sha="rework-sha",
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=lambda **_: (_ for _ in ()).throw(
            AssertionError("review approval at current rework head should be reused"),
        ),
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None and outcome.status == "ok"
    assert actions_taken == ["Review exchange passed (cached)"]
    assert job_runner.submitted == []


def test_cached_review_ignored_when_actual_worktree_head_moves_past_cache(
    tmp_path: Path,
) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_rework_validation = tmp_path / "rework-validation.json"
    _write_validation_record(cached_rework_validation, head_sha="rework-sha")
    _store_cached_approval(session_output, tmp_path, cached_rework_validation)

    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=277,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=cached_rework_validation),
        current_head_sha="new-sha",
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=lambda **_: (_ for _ in ()).throw(
            AssertionError("fresh exchange should be deferred to background job"),
        ),
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert len(job_runner.submitted) == 1


def test_cached_review_halt_is_logged_when_reused(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha")
    _write_validation_record(current_validation, head_sha="same-sha")
    _store_cached_halt(session_output, tmp_path, cached_validation)
    caplog.set_level(
        logging.INFO,
        logger="issue_orchestrator.control.completion_review_exchange",
    )

    errors: list[str] = []
    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=errors,
        actions_taken=actions_taken,
        run_review_exchange_loop=lambda **_: (_ for _ in ()).throw(
            AssertionError("cached halt should be reused"),
        ),
    )

    assert deferred is False
    assert halt is True
    assert completed is True
    assert outcome is not None and outcome.status == "stopped"
    assert actions_taken == []
    assert errors == [
        "review_exchange: stopped (max_rounds_exceeded)",
    ]
    halt_message = next(
        record.getMessage()
        for record in caplog.records
        if "[REVIEW_EXCHANGE] halt accepted" in record.getMessage()
    )
    assert "issue=230" in halt_message
    assert "session=coding-1" in halt_message
    assert "cached=True" in halt_message
    assert "status=stopped" in halt_message
    assert "reason=max_rounds_exceeded" in halt_message
    assert "rounds=3" in halt_message
    assert "head_sha=same-sha" in halt_message
    assert (
        f"summary_path={session_output.run_dir / 'review-exchange' / 'summary.json'}"
        in halt_message
    )
    assert "reviewer_response_text='Max rounds reached.'" in halt_message


def test_cached_review_is_ignored_when_validation_sha_differs(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="old-sha")
    _write_validation_record(current_validation, head_sha="new-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_stale_no_completion_summary_still_trips_loop_budget(tmp_path: Path) -> None:
    """The no-completion cap must win even when a stale summary cannot cache-hit."""
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )
    session_output.no_completion_count = 3

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="stale-sha")
    _write_validation_record(current_validation, head_sha="current-sha")
    review_run = session_output.cached_review_run()
    review_run.assets.validation_record_path.write_text(cached_validation.read_text())
    session_output.store_review_exchange_summary(
        review_run,
        _summary(
            status="error",
            reason="reviewer_no_completion",
            rounds=1,
        ),
    )

    errors: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=lambda **_: (_ for _ in ()).throw(
            AssertionError("loop budget must halt before another exchange starts"),
        ),
    )

    assert deferred is False
    assert halt is True
    assert completed is False
    assert outcome is None
    assert job_runner.submitted == []
    assert any(
        "3 consecutive reviewer/coder no-completion failures" in err for err in errors
    )


def test_cached_review_before_scratch_boundary_is_ignored(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha")
    _write_validation_record(current_validation, head_sha="same-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
        review_cache_boundary_started_at="future-boundary",
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


@pytest.mark.parametrize(
    ("cached_record_text", "case_id"),
    [
        (None, "missing-file"),
        ("{", "malformed-json"),
        (json.dumps({"passed": True}), "missing-head-sha"),
        (json.dumps({"passed": True, "head_sha": ""}), "empty-head-sha"),
    ],
)
def test_cached_review_is_ignored_without_matching_cached_sha_even_when_validation_not_required(
    tmp_path: Path,
    cached_record_text: str | None,
    case_id: str,
) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=False,
    )

    cached_validation = tmp_path / f"cached-validation-{case_id}.json"
    current_validation = tmp_path / "current-validation.json"
    if cached_record_text is not None:
        cached_validation.write_text(cached_record_text)
    _write_validation_record(current_validation, head_sha="new-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_cached_review_is_ignored_when_current_validation_sha_is_unavailable(
    tmp_path: Path,
) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="cached-sha")
    current_validation.write_text(json.dumps({"passed": True}))
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_cached_review_is_ignored_when_current_validation_failed_on_same_sha(
    tmp_path: Path,
) -> None:
    """Same-SHA cache hit must be rejected when the current validation
    explicitly failed. Without this guard the validation-failed-after-approval
    reroute would replay the cached approval forever (see #6086)."""
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha", passed=True)
    _write_validation_record(current_validation, head_sha="same-sha", passed=False)
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError(
            "fresh exchange should be deferred to background job, "
            "not replayed from the stale cached approval"
        )

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_no_job_runner_falls_back_to_inline_execution(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Construct without a job runner — the NullBackgroundJobRunner default must
    # short-circuit to inline execution so tests without async wiring still pass.
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    session_output = _FakeSessionOutput(tmp_path)
    caplog.set_level(
        logging.INFO,
        logger="issue_orchestrator.control.completion_review_exchange",
    )

    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, session_output),
        emit_review_started=lambda **kw: started.append(kw),
        emit_review_outcome=lambda **kw: outcomes.append(kw),
        review_exchange_runner=_FakeReviewExchangeRunner(),
    )

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        exchange_run = kwargs["exchange_run"]
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="reviewer_ok",
            run_assets=exchange_run.assets,
            reviewer_response=ReviewExchangeResponse(
                response_type="ok", getting_closer=True, response_text="Looks good."
            ),
            summary=_summary(
                status="ok",
                reason="reviewer_ok",
                rounds=1,
                response_text="Looks good.",
            ),
        )

    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None
    approval_message = next(
        record.getMessage()
        for record in caplog.records
        if "[REVIEW_EXCHANGE] approval accepted" in record.getMessage()
    )
    assert "issue=230" in approval_message
    assert "cached=False" in approval_message
    assert "reviewer_response_text='Looks good.'" in approval_message


def test_inline_review_exchange_halt_is_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    session_output = _FakeSessionOutput(tmp_path)
    caplog.set_level(
        logging.INFO,
        logger="issue_orchestrator.control.completion_review_exchange",
    )

    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, session_output),
        emit_review_started=lambda **kw: started.append(kw),
        emit_review_outcome=lambda **kw: outcomes.append(kw),
        review_exchange_runner=_FakeReviewExchangeRunner(),
    )

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        exchange_run = kwargs["exchange_run"]
        return ReviewExchangeOutcome(
            status="stopped",
            rounds=3,
            reason="max_rounds_exceeded",
            run_assets=exchange_run.assets,
            reviewer_response=ReviewExchangeResponse(
                response_type="changes_requested",
                getting_closer=True,
                response_text="Still not done.",
            ),
            summary=_summary(
                status="stopped",
                reason="max_rounds_exceeded",
                rounds=3,
                response_text="Still not done.",
            ),
        )

    errors: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is True
    assert completed is True
    assert outcome is not None and outcome.status == "stopped"
    assert errors == [
        "review_exchange: stopped (max_rounds_exceeded)",
    ]
    halt_message = next(
        record.getMessage()
        for record in caplog.records
        if "[REVIEW_EXCHANGE] halt accepted" in record.getMessage()
    )
    assert "issue=230" in halt_message
    assert "cached=False" in halt_message
    assert "status=stopped" in halt_message
    assert "reason=max_rounds_exceeded" in halt_message
    assert "rounds=3" in halt_message
    assert "reviewer_response_text='Still not done.'" in halt_message


def test_retry_does_not_reconsume_prior_run_timeout_cancellation(
    tmp_path: Path,
) -> None:
    """Regression for #6675.

    A coding run defers a review exchange, then the visible session times out
    and the timeout path cancels the background job with reason
    ``session-timeout``. A retry with the *same session name* but a *fresh run*
    (new ``run_id``) must NOT reconsume that stale cancellation and halt —
    which previously produced a spurious ``blocked-failed``. Instead the retry
    starts its own run-scoped exchange and defers.
    """
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )
    from issue_orchestrator.control.completion_review_exchange import (
        is_review_exchange_job_for_issue,
    )

    job_runner = _FakeJobRunner()
    supervisor = BackgroundJobSupervisor(job_runner)

    @dataclass(frozen=True)
    class _Cancellation:
        cancelled_job_ids: tuple[str, ...]

    def cancel(issue_number: int, reason: str) -> _Cancellation:
        cancelled = tuple(
            supervisor.cancel_matching(
                lambda job_id: is_review_exchange_job_for_issue(job_id, issue_number),
                reason=reason,
            )
        )
        return _Cancellation(cancelled_job_ids=cancelled)

    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, _FakeSessionOutput(tmp_path)),
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_FakeReviewExchangeRunner(),
        job_supervisor=supervisor,
        review_exchange_canceller=cancel,
    )

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must defer; loop must not run on this thread")

    # Original run (run-a) defers a background exchange.
    (_, _, _, _, _, first_deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        run_id="run-a",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )
    assert first_deferred is True
    assert job_runner.submitted[0][0] == "review-exchange:230:coding-1:run-a"

    # Visible session times out: the issue-scoped cancel still terminates the
    # live run-a job (acceptance criterion 4).
    cancellation = cancel(230, "session-timeout")
    assert cancellation.cancelled_job_ids == ("review-exchange:230:coding-1:run-a",)

    # Retry: same session name, fresh run (run-b). Must NOT halt on the stale
    # run-a cancellation; must start its own run-scoped exchange and defer.
    retry_errors: list[str] = []
    (_, _, retry_outcome, retry_completed, retry_halt, retry_deferred) = (
        review.prepare_review_exchange(
            requested_actions=(RequestedAction.CREATE_PR,),
            worktree=tmp_path,
            issue_number=230,
            issue_title="Example",
            session_name="coding-1",
            run_id="run-b",
            agent_label="agent:backend",
            record=_make_record(),
            errors=retry_errors,
            actions_taken=[],
            run_review_exchange_loop=fake_loop,
        )
    )

    assert retry_halt is False
    assert retry_deferred is True
    assert retry_completed is False
    assert retry_outcome is None
    # No stale cancellation leaked into the retry's error surface.
    assert not any("background job cancelled" in err for err in retry_errors)
    # The retry submitted its own run-scoped job, distinct from run-a's.
    submitted_ids = [job_id for job_id, _ in job_runner.submitted]
    assert submitted_ids == [
        "review-exchange:230:coding-1:run-a",
        "review-exchange:230:coding-1:run-b",
    ]
