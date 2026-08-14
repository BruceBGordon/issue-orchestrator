"""Consume :class:`BackgroundJobRunner` completions from the main tick.

The raw runner exposes a polling surface (``is_running`` / ``drain_completed``)
but no callers actually drained it, so background job **failures** never
reached the control layer. A crashed review-exchange thread would exit, the
tick would observe no running job and no ``summary.json``, and
:meth:`CompletionReviewExchange.run_review_exchange_if_needed` would happily
resubmit the same job — forever, once per tick, silently. This supervisor
plugs that gap.

Contract:

* :meth:`tick` is called once per main-loop iteration (or whenever the
  orchestrator reaches a safe point to reap completions). It drains the
  runner and records any exceptions under the corresponding ``job_id``.
* :meth:`take_failure` returns and clears a recorded failure so callers
  can surface it as a terminal outcome (e.g. mark the session FAILED)
  instead of spawning a fresh attempt.
* ``submit`` / ``is_running`` delegate to the underlying runner so
  consumers depend on the supervisor and nothing else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..ports.background_job import BackgroundJobRunner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundJobFailure:
    """Terminal error captured for one job_id."""

    job_id: str
    error: BaseException
    recorded_at: float  # Epoch seconds


@dataclass(frozen=True)
class BackgroundJobStatus:
    """Observable supervisor-owned state for one background job."""

    job_id: str
    running: bool
    started_at: float | None
    elapsed_seconds: float | None
    timeout_seconds: float | None
    deadline_exceeded: bool
    failure_recorded: bool
    cancelled: bool


@dataclass(frozen=True)
class _RunningJob:
    """Supervisor-owned metadata for one accepted background job."""

    job_id: str
    started_at: float
    timeout_seconds: float | None


class BackgroundJobTimeoutError(TimeoutError):
    """Raised by the supervisor when a job outlives its hard deadline."""

    def __init__(self, job_id: str, *, elapsed_seconds: float, timeout_seconds: float) -> None:
        self.job_id = job_id
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"background job exceeded deadline: job_id={job_id} "
            f"elapsed={elapsed_seconds:.1f}s timeout={timeout_seconds:.1f}s"
        )


class BackgroundJobCancelledError(RuntimeError):
    """Raised by the supervisor when an operator cancels a running job."""

    def __init__(self, job_id: str, *, reason: str) -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(
            f"background job cancelled: job_id={job_id} reason={reason}"
        )


class BackgroundJobSupervisor:
    """Owner of the ``BackgroundJobRunner`` failure-handling contract."""

    def __init__(
        self,
        runner: BackgroundJobRunner,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._failures: dict[str, BackgroundJobFailure] = {}
        self._running: dict[str, _RunningJob] = {}
        self._cancelled: set[str] = set()

    def submit(
        self,
        job_id: str,
        fn: Callable[[], None],
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        if job_id in self._cancelled and self._runner.is_running(job_id):
            logger.info("[BG] submit rejected (job is cancelling): %s", job_id)
            return False
        self._cancelled.discard(job_id)
        accepted = self._runner.submit(job_id, fn)
        if accepted:
            self._failures.pop(job_id, None)
            self._running[job_id] = _RunningJob(
                job_id=job_id,
                started_at=self._clock(),
                timeout_seconds=timeout_seconds if timeout_seconds and timeout_seconds > 0 else None,
            )
        return accepted

    def is_running(self, job_id: str) -> bool:
        return self.status(job_id).running

    def status(self, job_id: str) -> BackgroundJobStatus:
        """Return the current supervisor view for ``job_id``.

        This is the matrix input for callers that need to distinguish
        "still running inside its deadline" from "running with no bound" or
        "deadline failure already recorded." It intentionally reports
        supervisor metadata instead of exposing the raw runner/thread.
        """
        self._record_deadline_failures(job_id)
        running = False if job_id in self._cancelled else self._runner.is_running(job_id)
        metadata = self._running.get(job_id)
        elapsed_seconds: float | None = None
        deadline_exceeded = False
        if metadata is not None:
            elapsed_seconds = self._clock() - metadata.started_at
            deadline_exceeded = (
                metadata.timeout_seconds is not None
                and elapsed_seconds > metadata.timeout_seconds
            )
        return BackgroundJobStatus(
            job_id=job_id,
            running=running,
            started_at=metadata.started_at if metadata is not None else None,
            elapsed_seconds=elapsed_seconds,
            timeout_seconds=(
                metadata.timeout_seconds if metadata is not None else None
            ),
            deadline_exceeded=deadline_exceeded,
            failure_recorded=job_id in self._failures,
            cancelled=job_id in self._cancelled,
        )

    def cancel(self, job_id: str, *, reason: str) -> bool:
        """Record *job_id* as cancelled and stop waiting on it.

        Python threads cannot be killed safely. Cancellation therefore has two
        halves: callers must release/terminate the external resources owned by
        the job, and the supervisor records a terminal cancellation so the
        main loop does not keep reporting "still running" while that worker
        unwinds.
        """
        if not self._runner.is_running(job_id):
            self._running.pop(job_id, None)
            return False
        self._running.pop(job_id, None)
        self._cancelled.add(job_id)
        self._failures[job_id] = BackgroundJobFailure(
            job_id=job_id,
            error=BackgroundJobCancelledError(job_id, reason=reason),
            recorded_at=self._clock(),
        )
        logger.info("[BG] job_id=%s cancelled: %s", job_id, reason)
        return True

    def has_matching(self, predicate: Callable[[str], bool]) -> bool:
        """Report whether any supervised job matches *predicate* (non-mutating).

        Reads the same ``_running`` set :meth:`cancel_matching` iterates, so a
        lifecycle boundary's activity check and its cancellation can never drift
        on which jobs count as in-flight.
        """
        return any(predicate(job_id) for job_id in self._running)

    def cancel_matching(
        self,
        predicate: Callable[[str], bool],
        *,
        reason: str,
    ) -> list[str]:
        """Cancel all supervised jobs whose job_id matches *predicate*."""
        job_ids = [
            job_id
            for job_id in sorted(self._running)
            if predicate(job_id)
        ]
        return [job_id for job_id in job_ids if self.cancel(job_id, reason=reason)]

    def wait_until_stopped(
        self,
        job_ids: list[str] | tuple[str, ...],
        *,
        timeout_seconds: float = 5.0,
        poll_seconds: float = 0.01,
    ) -> bool:
        """Wait for cancelled workers to stop writing before destructive cleanup."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while any(self._runner.is_running(job_id) for job_id in job_ids):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(max(0.001, poll_seconds), remaining))
        return True

    def tick(self) -> None:
        """Drain completed jobs; store any failures keyed by job_id."""
        for done in self._runner.drain_completed():
            self._running.pop(done.job_id, None)
            if done.job_id in self._cancelled:
                # The cancellation failure is the terminal fact we want the
                # control layer to surface. The worker's eventual unwind may
                # report a low-level PTY or process error caused by the
                # cancellation itself; do not overwrite the operator action
                # with that secondary effect.
                self._cancelled.discard(done.job_id)
                continue
            if done.error is None:
                continue
            self._failures[done.job_id] = BackgroundJobFailure(
                job_id=done.job_id,
                error=done.error,
                recorded_at=self._clock(),
            )
            logger.warning(
                "[BG] job_id=%s failed: %s",
                done.job_id,
                done.error,
            )
        self._record_deadline_failures()

    def take_failure(self, job_id: str) -> BackgroundJobFailure | None:
        """Return and clear any recorded failure for *job_id*.

        Callers that see a failure here MUST NOT resubmit the same job_id
        without first either surfacing the error as a terminal outcome or
        making a deliberate retry decision. The supervisor deliberately
        "forgets" normal failures after returning them, so callers hold the
        responsibility for escalation. Deadline failures are kept while the
        job remains known to the supervisor; otherwise a later tick could
        silently resume waiting on the same over-deadline job.
        """
        self._record_deadline_failures(job_id)
        failure = self._failures.get(job_id)
        if isinstance(getattr(failure, "error", None), BackgroundJobTimeoutError):
            return failure
        return self._failures.pop(job_id, None)

    def _record_deadline_failures(self, job_id: str | None = None) -> None:
        now = self._clock()
        candidates = (
            [self._running[job_id]]
            if job_id is not None and job_id in self._running
            else list(self._running.values())
        )
        for running in candidates:
            timeout = running.timeout_seconds
            if timeout is None:
                continue
            elapsed = now - running.started_at
            if elapsed <= timeout or running.job_id in self._failures:
                continue
            self._failures[running.job_id] = BackgroundJobFailure(
                job_id=running.job_id,
                error=BackgroundJobTimeoutError(
                    running.job_id,
                    elapsed_seconds=elapsed,
                    timeout_seconds=timeout,
                ),
                recorded_at=now,
            )
            logger.error(
                "[BG] job_id=%s exceeded deadline elapsed=%.1fs timeout=%.1fs",
                running.job_id,
                elapsed,
                timeout,
            )

    def wait_until_idle(self, timeout: float = 60.0) -> bool:
        """Optional shutdown hook: block until no jobs are running.

        Delegates to the underlying runner when it exposes an idle check
        (``ThreadBackgroundJobRunner`` does); returns ``True`` immediately
        for synchronous adapters with no live state to drain.
        """
        hook = getattr(self._runner, "wait_until_idle", None)
        if not callable(hook):
            return True
        return bool(hook(timeout=timeout))
