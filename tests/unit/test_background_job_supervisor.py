"""End-to-end threaded tests for :class:`BackgroundJobSupervisor`.

The supervisor is the seam that turns a crashed background job into a
terminal outcome instead of an infinite resubmit loop. These tests drive
the real :class:`ThreadBackgroundJobRunner`, so they also cover the
threaded execution path that the earlier fake-runner tests did not.
"""

from __future__ import annotations

import threading

from issue_orchestrator.control.background_job_supervisor import (
    BackgroundJobCancelledError,
    BackgroundJobSupervisor,
    BackgroundJobTimeoutError,
)
from issue_orchestrator.execution.thread_background_job_runner import (
    ThreadBackgroundJobRunner,
)
from tests.callback_endpoint_helpers import ready_callback_endpoint
from issue_orchestrator.ports.timeline_evidence import NULL_TIMELINE_EVIDENCE


def test_successful_job_leaves_no_failure_record() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)
    done = threading.Event()

    assert supervisor.submit("ok-1", done.set) is True
    assert done.wait(timeout=5.0), "job never ran"
    assert runner.wait_until_idle(timeout=5.0)

    supervisor.tick()

    assert supervisor.take_failure("ok-1") is None
    assert supervisor.is_running("ok-1") is False


def test_job_failure_is_recorded_and_returned_once() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)
    started = threading.Event()

    def boom() -> None:
        started.set()
        raise RuntimeError("kaboom")

    assert supervisor.submit("bad-1", boom) is True
    assert started.wait(timeout=5.0)
    assert runner.wait_until_idle(timeout=5.0)

    supervisor.tick()

    failure = supervisor.take_failure("bad-1")
    assert failure is not None
    assert failure.job_id == "bad-1"
    assert isinstance(failure.error, RuntimeError)
    assert str(failure.error) == "kaboom"
    # `take_failure` clears the record — second call returns None.
    assert supervisor.take_failure("bad-1") is None


def test_resubmit_after_failure_runs_again_under_same_job_id() -> None:
    """After a failure has been taken, the job_id is free to be reused."""
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)
    first_ran = threading.Event()
    second_ran = threading.Event()

    def first_boom() -> None:
        first_ran.set()
        raise RuntimeError("nope")

    assert supervisor.submit("retry", first_boom) is True
    first_ran.wait(timeout=5.0)
    runner.wait_until_idle(timeout=5.0)
    supervisor.tick()
    assert supervisor.take_failure("retry") is not None

    # Caller decided to retry — new submission allowed.
    assert supervisor.submit("retry", second_ran.set) is True
    second_ran.wait(timeout=5.0)
    runner.wait_until_idle(timeout=5.0)
    supervisor.tick()
    assert supervisor.take_failure("retry") is None


def test_tick_is_idempotent_between_failures() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)

    def boom() -> None:
        raise RuntimeError("one shot")

    supervisor.submit("solo", boom)
    runner.wait_until_idle(timeout=5.0)
    supervisor.tick()
    supervisor.tick()  # no new completions; must not crash, must not duplicate
    failure = supervisor.take_failure("solo")
    assert failure is not None and isinstance(failure.error, RuntimeError)


def test_multiple_concurrent_jobs_each_record_their_own_failure() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)

    def make_boom(label: str):
        def boom() -> None:
            raise ValueError(label)
        return boom

    for job_id in ("a", "b", "c"):
        supervisor.submit(job_id, make_boom(job_id))
    runner.wait_until_idle(timeout=5.0)
    supervisor.tick()

    recovered = {jid: supervisor.take_failure(jid) for jid in ("a", "b", "c")}
    assert all(f is not None for f in recovered.values())
    assert {str(f.error) for f in recovered.values() if f is not None} == {"a", "b", "c"}


def test_running_job_deadline_is_reported_as_failure() -> None:
    runner = ThreadBackgroundJobRunner()
    now = 1000.0
    supervisor = BackgroundJobSupervisor(runner, clock=lambda: now)
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(timeout=5.0)

    assert supervisor.submit("slow", block, timeout_seconds=10.0) is True
    assert started.wait(timeout=5.0)

    now = 1011.0
    supervisor.tick()

    failure = supervisor.take_failure("slow")
    assert failure is not None
    assert isinstance(failure.error, BackgroundJobTimeoutError)
    assert "slow" in str(failure.error)

    release.set()
    assert runner.wait_until_idle(timeout=5.0)


def test_status_reports_running_job_deadline_metadata() -> None:
    runner = ThreadBackgroundJobRunner()
    now = 1000.0
    supervisor = BackgroundJobSupervisor(runner, clock=lambda: now)
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(timeout=5.0)

    assert supervisor.submit("slow", block, timeout_seconds=10.0) is True
    assert started.wait(timeout=5.0)

    now = 1004.5
    status = supervisor.status("slow")
    assert status.running is True
    assert status.elapsed_seconds == 4.5
    assert status.timeout_seconds == 10.0
    assert status.deadline_exceeded is False
    assert status.failure_recorded is False

    now = 1011.0
    status = supervisor.status("slow")
    assert status.running is True
    assert status.deadline_exceeded is True
    assert status.failure_recorded is True

    release.set()
    assert runner.wait_until_idle(timeout=5.0)


def test_cancel_records_terminal_failure_without_waiting_for_thread() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(timeout=5.0)

    assert supervisor.submit("review-exchange:42:coding-1", block) is True
    assert started.wait(timeout=5.0)

    assert supervisor.cancel(
        "review-exchange:42:coding-1",
        reason="operator-terminated",
    ) is True

    assert supervisor.is_running("review-exchange:42:coding-1") is False
    failure = supervisor.take_failure("review-exchange:42:coding-1")
    assert failure is not None
    assert isinstance(failure.error, BackgroundJobCancelledError)
    assert failure.error.reason == "operator-terminated"

    release.set()
    assert runner.wait_until_idle(timeout=5.0)
    supervisor.tick()
    assert supervisor.take_failure("review-exchange:42:coding-1") is None


def test_cancel_does_not_hide_finished_failure_before_tick() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)
    started = threading.Event()

    def boom() -> None:
        started.set()
        raise RuntimeError("finished failure")

    assert supervisor.submit("review-exchange:42:coding-1", boom) is True
    assert started.wait(timeout=5.0)
    assert runner.wait_until_idle(timeout=5.0)

    assert supervisor.cancel(
        "review-exchange:42:coding-1",
        reason="operator-terminated",
    ) is False

    supervisor.tick()
    failure = supervisor.take_failure("review-exchange:42:coding-1")
    assert failure is not None
    assert isinstance(failure.error, RuntimeError)
    assert str(failure.error) == "finished failure"


def test_cancel_matching_cancels_only_matching_supervised_jobs() -> None:
    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)
    release = threading.Event()
    started = [threading.Event(), threading.Event()]

    def make_blocker(index: int):
        def block() -> None:
            started[index].set()
            release.wait(timeout=5.0)
        return block

    assert supervisor.submit("review-exchange:42:coding-1", make_blocker(0)) is True
    assert supervisor.submit("review-exchange:99:coding-1", make_blocker(1)) is True
    assert started[0].wait(timeout=5.0)
    assert started[1].wait(timeout=5.0)

    cancelled = supervisor.cancel_matching(
        lambda job_id: job_id.startswith("review-exchange:42:"),
        reason="session-stopped",
    )

    assert cancelled == ["review-exchange:42:coding-1"]
    assert supervisor.is_running("review-exchange:42:coding-1") is False
    assert supervisor.is_running("review-exchange:99:coding-1") is True

    release.set()
    assert supervisor.wait_until_stopped(
        cancelled,
        timeout_seconds=5.0,
    ) is True

    release.set()
    assert runner.wait_until_idle(timeout=5.0)


def test_review_exchange_halts_when_supervisor_records_failure() -> None:
    """End-to-end: a background job raising → next tick returns a halt error."""
    # Local imports so this reads as an integration scenario, not a unit of
    # the supervisor itself.
    from pathlib import Path
    from issue_orchestrator.control.completion_review_exchange import (
        CompletionReviewExchange,
    )
    from issue_orchestrator.domain.models import (
        AgentConfig,
        CompletionOutcome,
        CompletionRecord,
        RequestedAction,
    )
    from issue_orchestrator.infra.config import Config

    class _Run:
        def __init__(self, run_dir: Path) -> None:
            self.run_dir = run_dir
            self.run_id = run_dir.name

    class _SessionOutput:
        def __init__(self, run_dir: Path) -> None:
            self._run_dir = run_dir

        def find_run_dir(self, worktree, session_name):  # noqa: ARG002
            return self._run_dir

        def load_review_exchange_summary(  # noqa: ARG002
            self,
            worktree,
            session_name,
            *,
            not_before_started_at=None,
        ):
            return None

        def count_consecutive_review_exchange_no_completion(  # noqa: ARG002
            self,
            worktree,
            session_name,
            *,
            not_before_started_at=None,
        ):
            # No no-completion failures recorded for this fake — the
            # bound never triggers, so the test exercises the
            # supervisor-failure halt path it's actually about.
            return 0

    runner = ThreadBackgroundJobRunner()
    supervisor = BackgroundJobSupervisor(runner)

    # Pre-record a failure; review exchange should surface it on next visit.
    # The job id is run-scoped (#6675): issue:session_name:run_id.
    supervisor.submit("review-exchange:42:coding-1:coding-run-1", lambda: _raise_boom())
    runner.wait_until_idle(timeout=5.0)
    supervisor.tick()

    cfg = Config(repo_root=Path("/tmp"))
    cfg.review_exchange_mode = "via-local-loop"
    cfg.code_review_agent = "agent:reviewer"
    cfg.agents = {
        "agent:backend": AgentConfig(
            prompt_path=Path("/tmp/backend.md"),
            command="claude",
            reviewer="agent:reviewer",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=Path("/tmp/reviewer.md"),
            command="claude",
        ),
    }
    class _RunnerStub:
        def run(self, **_: object):  # type: ignore[no-untyped-def]
            raise AssertionError("runner must not be invoked on halt path")

    review = CompletionReviewExchange(
        agent_callback_endpoint=ready_callback_endpoint(),
        timeline_evidence=NULL_TIMELINE_EVIDENCE,
        config=cfg,
        session_output=_SessionOutput(Path("/tmp")),  # type: ignore[arg-type]
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_RunnerStub(),  # type: ignore[arg-type]
        job_supervisor=supervisor,
    )
    errors: list[str] = []
    record = CompletionRecord(
        session_id="coding-1",
        timestamp="2026-04-17T00:00:00Z",
        outcome=CompletionOutcome.COMPLETED,
        summary="",
        implementation="",
        problems="",
        requested_actions=[RequestedAction.CREATE_PR],
    )
    (_, _, _, _, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=Path("/tmp"),
        issue_number=42,
        issue_title="test",
        session_name="coding-1",
        run_id="coding-run-1",
        agent_label="agent:backend",
        record=record,
        errors=errors,
        actions_taken=[],
        run_review_exchange_loop=lambda **_: (_ for _ in ()).throw(
            AssertionError("must not relaunch a crashed job"),
        ),
    )

    assert halt is True
    assert deferred is False
    assert any("background job raised" in err for err in errors)
    assert any("kaboom-from-thread" in err for err in errors)


def _raise_boom() -> None:
    raise RuntimeError("kaboom-from-thread")
