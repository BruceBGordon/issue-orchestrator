"""Review-exchange orchestration for completion processing."""

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..domain.models import CompletionRecord, RequestedAction
from ..domain.completion_finalization import ReviewExchangeRunningQuery
from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..domain.review_exchange_resume import ResumeDecision
from ..domain.review_artifacts import review_artifacts_from_exchange_result
from ..domain.timeline_evidence import (
    FinalizeTimelineEvidenceCommand,
    TimelineEvidenceIdentity,
)
from ..domain.runtime_config import RuntimeConfigReference
from ..ports.background_job import NullBackgroundJobRunner
from ..ports.review_exchange_runner import ReviewExchangeRunner
from ..ports.session_output import SessionOutput
from ..ports.timeline_evidence import TimelineEvidence
from .background_job_supervisor import (
    BackgroundJobCancelledError,
    BackgroundJobTimeoutError,
    BackgroundJobSupervisor,
)
from .completion_types import REVIEW_EXCHANGE_ERROR_PREFIX
from .review_exchange_cache_resolution import (
    ResumeResolution,
    ReuseResumeResolution,
    ReviewExchangeCacheResolver,
)
from .review_exchange_contracts import (
    ReviewExchangeCanceller,
    ReviewExchangeCancellationResult,
)
from .review_publish_pipeline import resolve_review_publish_pipeline


if TYPE_CHECKING:
    from ..infra.config import Config
    from ..domain.review_exchange import ReviewExchangeOutcome
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate

logger = logging.getLogger(__name__)

ReviewStartedEmitter = Callable[..., None]
ReviewOutcomeEmitter = Callable[..., None]
RunReviewExchangeLoop = Callable[..., "ReviewExchangeOutcome"]
_TerminalOutcomeMessage = Literal["approval accepted", "halt accepted"]


def _review_exchange_job_id(
    issue_number: int,
    session_name: str | None,
    run_id: str,
) -> str:
    """Stable, run-scoped job identity for a per-issue review exchange.

    The identity is ``review-exchange:{issue}:{session_name}:{run_id}``.
    Including ``run_id`` keeps repeated completion checks for the *same* coding
    run collapsed onto a single background thread (the run's ``run_id`` is
    stable across ticks) while a retry of the same session name — which always
    allocates a fresh run with a new ``run_id`` — starts its own exchange and
    never reconsumes the prior run's cancellation/failure state (#6675).

    ``session_name`` may be absent on degenerate paths that never submit a job
    (e.g. a running-check before a session name is known); the id then
    degrades to the coarser prefix, which no submitted job ever matches.
    """
    base = f"review-exchange:{issue_number}"
    if not session_name:
        return base
    return f"{base}:{session_name}:{run_id}"


def is_review_exchange_job_for_issue(job_id: str, issue_number: int) -> bool:
    """Return True when *job_id* belongs to *issue_number*'s exchange."""
    base = f"review-exchange:{issue_number}"
    return job_id == base or job_id.startswith(f"{base}:")


def _cached_review_event_metadata(
    exchange_outcome: "ReviewExchangeOutcome",
) -> dict[str, str]:
    if exchange_outcome.cache_metadata is None:
        return {}
    return exchange_outcome.cache_metadata.to_event_fields()


def _review_exchange_summary_path(
    exchange_outcome: "ReviewExchangeOutcome",
) -> str:
    if exchange_outcome.cache_metadata is not None:
        return str(exchange_outcome.cache_metadata.summary_path)
    return str(exchange_outcome.summary_path)


def _single_line_log_value(value: object, *, max_chars: int = 500) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _log_review_exchange_terminal_outcome(
    *,
    message: _TerminalOutcomeMessage,
    issue_number: int,
    session_name: str | None,
    exchange_outcome: "ReviewExchangeOutcome",
    review_run_dir: Path,
    cached: bool,
    log: Callable[..., None],
) -> None:
    summary = exchange_outcome.summary
    reviewer_text = (
        exchange_outcome.reviewer_response.response_text
        if exchange_outcome.reviewer_response is not None
        else summary.response_text
        if summary is not None
        else None
    )
    head_sha = summary.head_sha if summary is not None else None
    if not head_sha and exchange_outcome.cache_metadata is not None:
        head_sha = exchange_outcome.cache_metadata.head_sha
    validation_passed = summary.validation_passed if summary is not None else None
    log(
        "[REVIEW_EXCHANGE] %s "
        "issue=%d session=%s cached=%s status=%s reason=%s rounds=%s "
        "head_sha=%s validation_passed=%s summary_path=%s run_dir=%s "
        "reviewer_response_text=%r",
        message,
        issue_number,
        session_name,
        cached,
        exchange_outcome.status,
        exchange_outcome.reason,
        exchange_outcome.rounds,
        head_sha,
        validation_passed,
        _review_exchange_summary_path(exchange_outcome),
        review_run_dir,
        _single_line_log_value(reviewer_text),
    )


def _review_exchange_halt_error(
    exchange_outcome: "ReviewExchangeOutcome",
) -> str:
    return (
        f"{REVIEW_EXCHANGE_ERROR_PREFIX} {exchange_outcome.status} "
        f"({exchange_outcome.reason})"
    )


def _log_review_exchange_approval(
    *,
    issue_number: int,
    session_name: str | None,
    exchange_outcome: "ReviewExchangeOutcome",
    review_run_dir: Path,
    cached: bool,
) -> None:
    _log_review_exchange_terminal_outcome(
        message="approval accepted",
        issue_number=issue_number,
        session_name=session_name,
        exchange_outcome=exchange_outcome,
        review_run_dir=review_run_dir,
        cached=cached,
        log=logger.info,
    )


def _log_review_exchange_halt(
    *,
    issue_number: int,
    session_name: str | None,
    exchange_outcome: "ReviewExchangeOutcome",
    review_run_dir: Path,
    cached: bool,
) -> None:
    _log_review_exchange_terminal_outcome(
        message="halt accepted",
        issue_number=issue_number,
        session_name=session_name,
        exchange_outcome=exchange_outcome,
        review_run_dir=review_run_dir,
        cached=cached,
        log=logger.warning,
    )


class CompletionReviewExchange:
    """Owns review-exchange mode selection, caching, execution, and artifacts."""

    def __init__(
        self,
        *,
        config: "Config | None",
        session_output: SessionOutput,
        emit_review_started: ReviewStartedEmitter,
        emit_review_outcome: ReviewOutcomeEmitter,
        review_exchange_runner: ReviewExchangeRunner,
        job_supervisor: BackgroundJobSupervisor | None = None,
        review_exchange_canceller: ReviewExchangeCanceller | None = None,
        agent_callback_endpoint: "AgentCallbackEndpoint",
        timeline_evidence: TimelineEvidence,
    ) -> None:
        self._config = config
        self._session_output = session_output
        self._review_exchange_runner = review_exchange_runner
        self._emit_review_started = emit_review_started
        self._emit_review_outcome = emit_review_outcome
        self._review_exchange_canceller = review_exchange_canceller
        self._agent_callback_endpoint = agent_callback_endpoint
        self._timeline_evidence = timeline_evidence
        self._review_runs_lock = threading.Lock()
        self._review_runs_by_job_id: dict[str, ReviewExchangeRun] = {}
        # Supervisor injection is REQUIRED for the async failure path to work:
        # ``take_failure`` only returns values that ``tick()`` has populated,
        # and ``tick()`` must be called from the orchestrator's main loop.
        # Tests that exercise the async path inject a real supervisor and
        # drive ``tick()`` themselves. Tests that don't care about async pass
        # nothing — we default to a supervisor wrapping NullBackgroundJobRunner
        # so ``submit``/``is_running`` return no-op values and the failure
        # queue is always empty (no need to tick).
        self._job_supervisor = job_supervisor or BackgroundJobSupervisor(
            NullBackgroundJobRunner()
        )

    def prepare_review_exchange(
        self,
        *,
        requested_actions: tuple[RequestedAction, ...],
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        run_id: str,
        agent_label: str | None,
        record: CompletionRecord,
        errors: list[str],
        actions_taken: list[str],
        run_review_exchange_loop: RunReviewExchangeLoop,
        approval_gate: "ReviewExchangeApprovalGate | None" = None,
        review_cache_boundary_started_at: str | None = None,
        current_head_sha: str | None = None,
    ) -> tuple[Any, str | None, ReviewExchangeOutcome | None, bool, bool, bool]:
        """Resolve mode, run/poll review exchange, and report status.

        Returns (plan, exchange_mode, exchange_result, review_exchange_completed,
        exchange_halt, deferred). ``deferred=True`` means the exchange is
        running in the background and completion processing must retry on a
        later tick — the caller MUST NOT proceed to push/PR creation.
        """
        exchange_mode: str | None = None
        exchange_result: ReviewExchangeOutcome | None = None
        review_exchange_completed = False

        if RequestedAction.CREATE_PR in requested_actions:
            try:
                exchange_mode = self.resolve_review_exchange_mode(agent_label)
            except ValueError as exc:
                errors.append(f"{REVIEW_EXCHANGE_ERROR_PREFIX} {exc}")
                pipeline = resolve_review_publish_pipeline(None)
                return pipeline.plan(requested_actions), None, None, False, True, False

        pipeline = resolve_review_publish_pipeline(exchange_mode)
        plan = pipeline.plan(requested_actions)
        if not plan.run_review_exchange_before_publish:
            return (
                plan,
                exchange_mode,
                exchange_result,
                review_exchange_completed,
                False,
                False,
            )

        (
            exchange_mode,
            exchange_result,
            exchange_halt,
            deferred,
        ) = self.run_review_exchange_if_needed(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            run_id=run_id,
            agent_label=agent_label,
            initial_validation_record_path=(
                Path(record.validation_record_path)
                if record.validation_record_path
                else None
            ),
            current_head_sha=current_head_sha,
            review_cache_boundary_started_at=review_cache_boundary_started_at,
            errors=errors,
            actions_taken=actions_taken,
            run_review_exchange_loop=run_review_exchange_loop,
            approval_gate=approval_gate,
        )
        if exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result:
            review_exchange_completed = True
        return (
            plan,
            exchange_mode,
            exchange_result,
            review_exchange_completed,
            exchange_halt,
            deferred,
        )

    def resolve_create_pr_exchange_mode(
        self,
        *,
        exchange_mode: str | None,
        agent_label: str | None,
        errors: list[str],
    ) -> tuple[str | None, bool]:
        if exchange_mode is not None:
            return exchange_mode, False
        try:
            return self.resolve_review_exchange_mode(agent_label), False
        except ValueError as exc:
            errors.append(f"{REVIEW_EXCHANGE_ERROR_PREFIX} {exc}")
            return None, True

    def missing_review_exchange_outcome(
        self,
        exchange_mode: str | None,
        exchange_result: ReviewExchangeOutcome | None,
    ) -> bool:
        return (
            exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result is None
        )

    def is_review_exchange_running(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        run_id: str,
    ) -> bool:
        """Report whether a background review-exchange job is in flight.

        The reroute budget on the consumer side uses this to distinguish
        polling ticks (no new work, just waiting for an existing job) from
        fresh attempts. Counting polling ticks would let a slow exchange
        exhaust the budget before it finishes.

        ``run_id`` scopes the lookup to the current coding run so a retry never
        observes a prior run's job (#6675).
        """
        job_id = _review_exchange_job_id(issue_number, session_name, run_id)
        return self._job_supervisor.is_running(job_id)

    def is_review_exchange_running_for_completion(
        self,
        query: ReviewExchangeRunningQuery,
    ) -> bool:
        """Answer the typed running-job query used by finalization policy."""
        _require_review_exchange_running_query(query)
        if not query.requires_review_exchange:
            return False
        return self.is_review_exchange_running(
            issue_number=query.issue_number,
            session_name=query.session_name,
            run_id=query.run_id,
        )

    def cancel_issue(
        self,
        issue_number: int,
        reason: str,
    ) -> ReviewExchangeCancellationResult:
        """Cancel one issue's runtime and terminalize every owned review run."""
        return self._cancel_issue(issue_number, reason, outcome="cancelled")

    def _cancel_issue(
        self,
        issue_number: int,
        reason: str,
        *,
        outcome: str,
    ) -> ReviewExchangeCancellationResult:
        if self._review_exchange_canceller is None:
            raise RuntimeError("review-exchange cancellation owner is not configured")
        cancellation = self._review_exchange_canceller(issue_number, reason)
        with self._review_runs_lock:
            job_ids = tuple(
                job_id
                for job_id, review_run in self._review_runs_by_job_id.items()
                if review_run.issue_number == issue_number
            )
        for job_id in job_ids:
            self._finalize_background_review_run(job_id, outcome=outcome)
        return cancellation

    def is_review_exchange_within_deadline_for_completion(
        self,
        query: ReviewExchangeRunningQuery,
    ) -> bool:
        """Report whether the in-flight BG job is still inside its own deadline.

        The completion-finalization matrix uses this to decide whether a
        TIMED_OUT visible session should cancel the BG review-exchange or
        keep deferring. The BG job's deadline (``review_exchange_supervisor
        _timeout_seconds``) is *much* larger than the visible session's
        per-agent timeout (per-role * max_rounds + grace), so a healthy
        multi-round exchange routinely outlives the outer budget.

        Returns False when the job is not running, when it has been
        running long enough that the supervisor would flag the deadline
        as exceeded, or when the job has no supervisor deadline at all
        (``timeout_seconds is None``). The last case must fall through
        to the terminal-cancel path so the existing unbounded-job halt
        in ``run_review_exchange_if_needed`` still runs — otherwise a
        timed-out visible session would defer indefinitely against an
        unbounded BG job that has no deadline of its own to honor.
        """
        _require_review_exchange_running_query(query)
        if not query.requires_review_exchange:
            return False
        job_id = _review_exchange_job_id(
            query.issue_number, query.session_name, query.run_id
        )
        status = self._job_supervisor.status(job_id)
        return (
            status.running
            and status.timeout_seconds is not None
            and not status.deadline_exceeded
        )

    def run_review_exchange_if_needed(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        run_id: str,
        agent_label: str | None,
        initial_validation_record_path: Path | None,
        errors: list[str],
        actions_taken: list[str],
        run_review_exchange_loop: RunReviewExchangeLoop,
        approval_gate: "ReviewExchangeApprovalGate | None" = None,
        review_cache_boundary_started_at: str | None = None,
        current_head_sha: str | None = None,
    ) -> tuple[str | None, ReviewExchangeOutcome | None, bool, bool]:
        """Return (exchange_mode, outcome, halt, deferred).

        ``deferred=True`` means the exchange is running asynchronously; the
        caller must stop processing this completion record and retry on a
        later tick. ``outcome`` is populated only when the exchange has
        finished — from the on-disk cache of a prior background run, or
        from an inline run when no background runner is wired (tests, dev
        environments).
        """
        try:
            exchange_mode = self.resolve_review_exchange_mode(agent_label)
        except ValueError as exc:
            errors.append(f"{REVIEW_EXCHANGE_ERROR_PREFIX} {exc}")
            return None, None, True, False
        if exchange_mode not in {"via-mcp", "via-local-loop"}:
            return exchange_mode, None, False, False
        if not session_name:
            errors.append(
                f"{REVIEW_EXCHANGE_ERROR_PREFIX} review exchange requires session_name"
            )
            return exchange_mode, None, True, False
        coder_label = self.require_review_exchange_agent_label(
            agent_label, exchange_mode
        )
        reviewer_label = self.resolve_reviewer_label(coder_label)
        job_id = _review_exchange_job_id(issue_number, session_name, run_id)
        background_failure = self._take_background_failure(
            job_id=job_id,
            issue_number=issue_number,
            errors=errors,
        )
        if background_failure is not None:
            return exchange_mode, None, True, False
        require_validation = bool(
            self._config and self._config.review_exchange_require_validation
        )
        resolution = self.decide_review_exchange_resumption(
            worktree,
            session_name,
            require_validation=require_validation,
            current_validation_record_path=initial_validation_record_path,
            current_head_sha=current_head_sha,
            not_before_started_at=review_cache_boundary_started_at,
        )
        early = self._dispatch_resume_decision(
            resolution=resolution,
            exchange_mode=exchange_mode,
            issue_number=issue_number,
            session_name=session_name,
            worktree=worktree,
            reviewer_label=reviewer_label,
            errors=errors,
            actions_taken=actions_taken,
            review_cache_boundary_started_at=review_cache_boundary_started_at,
        )
        if early is not None:
            return early
        # IGNORE_STALE / NO_CACHE / INVALID_SUMMARY / under-budget
        # COUNT_NO_COMPLETION_AND_RETRY all reach here and fall
        # through to the fresh-exchange spawn below. Stale and
        # no-cache are benign (head moved, first run);
        # INVALID_SUMMARY is logged by the decide path and treated
        # as spawn-fresh so corrupted state doesn't strand the issue.
        loop_budget_halt = self._maybe_escalate_no_completion_budget(
            exchange_mode=exchange_mode,
            issue_number=issue_number,
            session_name=session_name,
            worktree=worktree,
            errors=errors,
            review_cache_boundary_started_at=review_cache_boundary_started_at,
        )
        if loop_budget_halt is not None:
            return loop_budget_halt

        if self._job_supervisor.is_running(job_id):
            return self._defer_running_review_exchange(
                exchange_mode=exchange_mode,
                job_id=job_id,
                issue_number=issue_number,
                errors=errors,
            )

        logger.info("Review exchange mode selected: %s", exchange_mode)
        review_run = self._start_review_exchange_run(
            worktree=worktree,
            issue_number=issue_number,
            parent_session_name=session_name,
            agent_label=coder_label,
        )
        self._emit_review_started(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            run_dir=review_run.assets.run_dir,
        )
        submitted = self._submit_background_review_exchange(
            job_id=job_id,
            exchange_mode=exchange_mode,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=coder_label,
            reviewer_label=reviewer_label,
            initial_validation_record_path=initial_validation_record_path,
            review_run=review_run,
            current_head_sha=current_head_sha,
            run_review_exchange_loop=run_review_exchange_loop,
            approval_gate=approval_gate,
        )
        if submitted:
            # Deferred: the exchange is now running off the main tick. A
            # subsequent tick will re-enter this path, find summary.json via
            # load_existing_review_exchange_outcome, and resume publish.
            return exchange_mode, None, False, True

        # No background runner wired — fall back to the legacy synchronous
        # path so tests and dev environments without a job runner still work.
        logger.debug("[REVIEW_EXCHANGE] no background runner; running exchange inline")
        mode, outcome, halt = self._run_fresh_review_exchange(
            exchange_mode=exchange_mode,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            reviewer_label=reviewer_label,
            initial_validation_record_path=initial_validation_record_path,
            review_run=review_run,
            current_head_sha=current_head_sha,
            errors=errors,
            actions_taken=actions_taken,
            run_review_exchange_loop=run_review_exchange_loop,
            approval_gate=approval_gate,
        )
        return mode, outcome, halt, False

    def _take_background_failure(
        self,
        *,
        job_id: str,
        issue_number: int,
        errors: list[str],
    ) -> Any | None:
        """Surface a recorded background failure as a terminal halt."""
        failure = self._job_supervisor.take_failure(job_id)
        if failure is None:
            return None
        self._finalize_background_review_run(job_id, outcome="error")
        cancel_error = self._cancel_runtime_after_background_failure(
            issue_number=issue_number,
            job_id=job_id,
            reason=self._background_failure_cancellation_reason(failure.error),
        )
        if isinstance(failure.error, BackgroundJobCancelledError):
            reason = f"{REVIEW_EXCHANGE_ERROR_PREFIX} background job cancelled: {failure.error.reason}"
            errors.append(reason)
            if cancel_error:
                errors.append(cancel_error)
            logger.info(
                "[REVIEW_EXCHANGE] background job cancelled; halting issue=%d job_id=%s reason=%s",
                issue_number,
                job_id,
                failure.error.reason,
            )
            return failure
        reason = (
            f"{REVIEW_EXCHANGE_ERROR_PREFIX} background job raised: {failure.error}"
        )
        errors.append(reason)
        if cancel_error:
            errors.append(cancel_error)
        logger.error(
            "[REVIEW_EXCHANGE] background job failed; halting issue=%d job_id=%s",
            issue_number,
            job_id,
            exc_info=(type(failure.error), failure.error, None),
        )
        return failure

    def _cancel_runtime_after_background_failure(
        self,
        *,
        issue_number: int,
        job_id: str,
        reason: str,
    ) -> str | None:
        """Tear down issue-scoped runtime work after a terminal job failure."""
        if self._review_exchange_canceller is None:
            logger.warning(
                "[REVIEW_EXCHANGE] no canceller configured for background "
                "failure issue=%d job_id=%s reason=%s",
                issue_number,
                job_id,
                reason,
            )
            return None
        try:
            cancellation = self._cancel_issue(
                issue_number,
                reason,
                outcome="error",
            )
        except Exception as exc:  # noqa: BLE001 - failure path must still halt visibly
            logger.exception(
                "[REVIEW_EXCHANGE] failed to cancel runtime after background "
                "failure issue=%d job_id=%s reason=%s",
                issue_number,
                job_id,
                reason,
            )
            return (
                f"{REVIEW_EXCHANGE_ERROR_PREFIX} failed to cancel runtime work: {exc}"
            )
        cancelled_jobs = cancellation.cancelled_job_ids
        logger.info(
            "[REVIEW_EXCHANGE] cancelled runtime after background failure "
            "issue=%d job_id=%s reason=%s jobs=%s",
            issue_number,
            job_id,
            reason,
            ",".join(cancelled_jobs) if cancelled_jobs else "none",
        )
        return None

    @staticmethod
    def _background_failure_cancellation_reason(error: BaseException) -> str:
        if isinstance(error, BackgroundJobCancelledError):
            return error.reason
        if isinstance(error, BackgroundJobTimeoutError):
            return "background-job-timeout"
        return "background-job-failed"

    def _defer_running_review_exchange(
        self,
        *,
        exchange_mode: str,
        job_id: str,
        issue_number: int,
        errors: list[str],
    ) -> tuple[str | None, ReviewExchangeOutcome | None, bool, bool]:
        """Return the running-job decision cell for the exchange matrix."""
        status_fn = getattr(self._job_supervisor, "status", None)
        status = status_fn(job_id) if callable(status_fn) else None
        if status is not None and getattr(status, "failure_recorded", False):
            background_failure = self._take_background_failure(
                job_id=job_id,
                issue_number=issue_number,
                errors=errors,
            )
            if background_failure is not None:
                return exchange_mode, None, True, False
        if status is not None and getattr(status, "timeout_seconds", None) is None:
            reason = (
                f"{REVIEW_EXCHANGE_ERROR_PREFIX} background job is running "
                f"without a supervisor deadline: job_id={job_id}"
            )
            errors.append(reason)
            cancel_error = self._cancel_runtime_after_background_failure(
                issue_number=issue_number,
                job_id=job_id,
                reason="background-job-unbounded",
            )
            if cancel_error:
                errors.append(cancel_error)
            self._finalize_background_review_run(job_id, outcome="error")
            logger.error(
                "[REVIEW_EXCHANGE] refusing unbounded background wait "
                "issue=%d job_id=%s elapsed=%s",
                issue_number,
                job_id,
                self._format_seconds(getattr(status, "elapsed_seconds", None)),
            )
            return exchange_mode, None, True, False
        if status is not None:
            elapsed = getattr(status, "elapsed_seconds", None)
            timeout = getattr(status, "timeout_seconds", None)
            deadline_in = timeout - elapsed if elapsed is not None and timeout else None
            logger.info(
                "[REVIEW_EXCHANGE] job still running issue=%d job_id=%s "
                "elapsed=%s timeout=%s deadline_in=%s — deferring",
                issue_number,
                job_id,
                self._format_seconds(elapsed),
                self._format_seconds(timeout),
                self._format_seconds(deadline_in),
            )
        else:
            logger.info(
                "[REVIEW_EXCHANGE] job still running issue=%d job_id=%s — deferring",
                issue_number,
                job_id,
            )
        return exchange_mode, None, False, True

    @staticmethod
    def _format_seconds(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}s"

    def _submit_background_review_exchange(
        self,
        *,
        job_id: str,
        exchange_mode: str,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        reviewer_label: str | None,
        initial_validation_record_path: Path | None,
        review_run: ReviewExchangeRun,
        current_head_sha: str | None,
        run_review_exchange_loop: RunReviewExchangeLoop,
        approval_gate: "ReviewExchangeApprovalGate | None",
    ) -> bool:
        """Start the review exchange in a background job; return True if accepted.

        The background closure mirrors ``_run_fresh_review_exchange`` up to the
        point of emitting the *started* event and handing off to
        ``run_review_exchange_loop``. The loop writes ``summary.json`` when it
        finishes, which is the signal the next tick uses to resume processing.
        """

        with self._review_runs_lock:
            self._review_runs_by_job_id[job_id] = review_run

        def _job() -> None:
            try:
                self._execute_terminal_review_run(
                    review_run=review_run,
                    current_head_sha=current_head_sha,
                    execute=lambda: run_review_exchange_loop(
                        exchange_run=review_run,
                        worktree=worktree,
                        issue_number=issue_number,
                        issue_title=issue_title,
                        session_name=session_name,
                        agent_label=agent_label,
                        initial_validation_record_path=initial_validation_record_path,
                        approval_gate=approval_gate,
                    ),
                )
            except Exception:
                logger.exception(
                    "[REVIEW_EXCHANGE] background job failed: issue=%d job_id=%s",
                    issue_number,
                    job_id,
                )
                raise
            finally:
                with self._review_runs_lock:
                    self._review_runs_by_job_id.pop(job_id, None)

        submitted = self._job_supervisor.submit(
            job_id,
            _job,
            timeout_seconds=self._review_exchange_job_timeout_seconds(agent_label),
        )
        if not submitted:
            with self._review_runs_lock:
                self._review_runs_by_job_id.pop(job_id, None)
        return submitted

    def _dispatch_resume_decision(
        self,
        *,
        resolution: ResumeResolution,
        exchange_mode: str,
        issue_number: int,
        session_name: str | None,
        worktree: Path,
        reviewer_label: str | None,
        errors: list[str],
        actions_taken: list[str],
        review_cache_boundary_started_at: str | None,
    ) -> tuple[str | None, ReviewExchangeOutcome | None, bool, bool] | None:
        """Translate a ``ResumeResolution`` into the early-return tuple
        that ``run_review_exchange_if_needed`` expects, or ``None`` to
        signal "fall through and spawn a fresh exchange."

        Extracted to keep the parent function under the C901 ceiling
        and to make the dispatch matrix readable at a glance: each
        cache-hit decision is one branch; the budget consultation for
        the no-completion case lives next to the variant that asks
        about it.
        """
        decision = resolution.decision
        if decision is ResumeDecision.REUSE_APPROVAL:
            if not isinstance(resolution, ReuseResumeResolution):
                raise TypeError("REUSE_APPROVAL requires ReuseResumeResolution")
            mode, outcome, halt = self._handle_cached_review_exchange_outcome(
                exchange_mode=exchange_mode,
                existing_outcome=resolution.cached.outcome,
                run_assets=resolution.cached.run_assets,
                issue_number=issue_number,
                session_name=session_name,
                reviewer_label=reviewer_label,
                errors=errors,
                actions_taken=actions_taken,
            )
            return mode, outcome, halt, False
        if decision is ResumeDecision.REUSE_HALT:
            if not isinstance(resolution, ReuseResumeResolution):
                raise TypeError("REUSE_HALT requires ReuseResumeResolution")
            mode, outcome, halt = self._handle_cached_review_exchange_outcome(
                exchange_mode=exchange_mode,
                existing_outcome=resolution.cached.outcome,
                run_assets=resolution.cached.run_assets,
                issue_number=issue_number,
                session_name=session_name,
                reviewer_label=reviewer_label,
                errors=errors,
                actions_taken=actions_taken,
            )
            return mode, outcome, halt, False
        if decision is ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY:
            return self._maybe_escalate_no_completion_budget(
                exchange_mode=exchange_mode,
                issue_number=issue_number,
                session_name=session_name,
                worktree=worktree,
                errors=errors,
                review_cache_boundary_started_at=review_cache_boundary_started_at,
            )
        # Stale / no-cache / invalid-summary → spawn fresh.
        return None

    def _maybe_escalate_no_completion_budget(
        self,
        *,
        exchange_mode: str,
        issue_number: int,
        session_name: str | None,
        worktree: Path,
        errors: list[str],
        review_cache_boundary_started_at: str | None,
    ) -> tuple[str | None, ReviewExchangeOutcome | None, bool, bool] | None:
        """Consult the no-completion budget. Returns the early-halt
        tuple if the threshold has been reached; ``None`` to let the
        caller spawn a fresh exchange."""
        max_failures = self._max_consecutive_review_exchange_failures()
        if session_name is None or max_failures <= 0:
            return None
        consecutive = (
            self._session_output.count_consecutive_review_exchange_no_completion(
                worktree,
                session_name,
                not_before_started_at=review_cache_boundary_started_at,
            )
        )
        if consecutive < max_failures:
            return None
        logger.error(
            "[REVIEW_EXCHANGE] no-completion runaway detected; "
            "halting issue=%d session=%s consecutive=%d max=%d",
            issue_number,
            session_name,
            consecutive,
            max_failures,
        )
        errors.append(
            f"{REVIEW_EXCHANGE_ERROR_PREFIX} {consecutive} consecutive "
            f"reviewer/coder no-completion failures (max {max_failures}) — "
            "escalating to needs-human"
        )
        return exchange_mode, None, True, False

    def _handle_cached_review_exchange_outcome(
        self,
        *,
        exchange_mode: str,
        existing_outcome: ReviewExchangeOutcome,
        run_assets: ReviewExchangeRunAssets,
        issue_number: int,
        session_name: str | None,
        reviewer_label: str | None,
        errors: list[str],
        actions_taken: list[str],
    ) -> tuple[str, ReviewExchangeOutcome, bool]:
        review_run_dir = run_assets.run_dir
        cache_metadata = _cached_review_event_metadata(existing_outcome)
        self._emit_review_started(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            run_dir=review_run_dir,
            cached=True,
            **cache_metadata,
        )
        if existing_outcome.status == "ok":
            actions_taken.append("Review exchange passed (cached)")
            _log_review_exchange_approval(
                issue_number=issue_number,
                session_name=session_name,
                exchange_outcome=existing_outcome,
                review_run_dir=review_run_dir,
                cached=True,
            )
            reviewer_summary = (
                existing_outcome.reviewer_response.response_text
                if getattr(existing_outcome, "reviewer_response", None)
                else "Review approved via cached exchange summary"
            )
            self._emit_review_outcome(
                issue_number=issue_number,
                reviewer_label=reviewer_label,
                exchange_mode=exchange_mode,
                approved=True,
                rounds=getattr(existing_outcome, "rounds", None),
                summary=reviewer_summary,
                run_dir=review_run_dir,
                cached=True,
                artifacts=self._review_artifacts_from_outcome(existing_outcome),
                **cache_metadata,
            )
            return exchange_mode, existing_outcome, False
        _log_review_exchange_halt(
            issue_number=issue_number,
            session_name=session_name,
            exchange_outcome=existing_outcome,
            review_run_dir=review_run_dir,
            cached=True,
        )
        self._emit_review_outcome(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            approved=False,
            rounds=getattr(existing_outcome, "rounds", None),
            summary=f"Review exchange halted: {existing_outcome.reason}",
            run_dir=review_run_dir,
            cached=True,
            artifacts=self._review_artifacts_from_outcome(existing_outcome),
            **cache_metadata,
        )
        errors.append(_review_exchange_halt_error(existing_outcome))
        return exchange_mode, existing_outcome, True

    def _run_fresh_review_exchange(
        self,
        *,
        exchange_mode: str,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        reviewer_label: str | None,
        initial_validation_record_path: Path | None,
        review_run: ReviewExchangeRun,
        current_head_sha: str | None,
        errors: list[str],
        actions_taken: list[str],
        run_review_exchange_loop: RunReviewExchangeLoop,
        approval_gate: "ReviewExchangeApprovalGate | None",
    ) -> tuple[str, ReviewExchangeOutcome, bool]:
        exchange_result = self._execute_terminal_review_run(
            review_run=review_run,
            current_head_sha=current_head_sha,
            execute=lambda: run_review_exchange_loop(
                exchange_run=review_run,
                worktree=worktree,
                issue_number=issue_number,
                issue_title=issue_title,
                session_name=session_name,
                agent_label=agent_label,
                initial_validation_record_path=initial_validation_record_path,
                approval_gate=approval_gate,
            ),
        )
        run_assets = review_run.assets
        review_run_dir = run_assets.run_dir
        if exchange_result.status != "ok":
            _log_review_exchange_halt(
                issue_number=issue_number,
                session_name=session_name,
                exchange_outcome=exchange_result,
                review_run_dir=review_run_dir,
                cached=False,
            )
            self._emit_review_outcome(
                issue_number=issue_number,
                reviewer_label=reviewer_label,
                exchange_mode=exchange_mode,
                approved=False,
                rounds=getattr(exchange_result, "rounds", None),
                summary=f"Review exchange halted: {exchange_result.reason}",
                run_dir=review_run_dir,
                artifacts=self._review_artifacts_from_outcome(exchange_result),
            )
            errors.append(_review_exchange_halt_error(exchange_result))
            return exchange_mode, exchange_result, True

        actions_taken.append("Review exchange passed")
        _log_review_exchange_approval(
            issue_number=issue_number,
            session_name=session_name,
            exchange_outcome=exchange_result,
            review_run_dir=review_run_dir,
            cached=False,
        )
        reviewer_summary = (
            exchange_result.reviewer_response.response_text
            if exchange_result.reviewer_response
            else "Review approved"
        )
        self._emit_review_outcome(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            approved=True,
            rounds=exchange_result.rounds,
            summary=reviewer_summary,
            run_dir=review_run_dir,
            artifacts=self._review_artifacts_from_outcome(exchange_result),
        )
        return exchange_mode, exchange_result, False

    def _max_consecutive_review_exchange_failures(self) -> int:
        """Return the configured cap on consecutive ``*_no_completion``
        failures before escalating, or 0 if config is absent (in which
        case the bound is disabled)."""
        if self._config is None:
            return 0
        max_failures = getattr(
            self._config,
            "max_consecutive_review_exchange_failures",
            0,
        )
        if not isinstance(max_failures, int) or max_failures < 0:
            return 0
        return max_failures

    @staticmethod
    def _review_artifacts_from_outcome(
        exchange_result: "ReviewExchangeOutcome",
    ) -> list[dict[str, str]] | None:
        return review_artifacts_from_exchange_result(exchange_result) or None

    def _review_exchange_job_timeout_seconds(
        self, agent_label: str | None
    ) -> float | None:
        """Return a hard wall-clock deadline for one deferred exchange job.

        The runner owns the round/retry budget and exposes the derived
        supervisor deadline through the injected port. Control only resolves
        the active coder/reviewer configs.
        """
        if self._config is None or not agent_label:
            return None
        try:
            reviewer_label = self.resolve_reviewer_label(agent_label)
        except ValueError:
            return None
        coder_agent = self._config.agents.get(agent_label)
        reviewer_agent = self._config.agents.get(reviewer_label)
        if coder_agent is None or reviewer_agent is None:
            return None
        max_rounds = max(1, int(getattr(self._config, "review_exchange_max_rounds", 1)))
        return self._review_exchange_runner.job_timeout_seconds(
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            max_rounds=max_rounds,
        )

    def _start_review_exchange_run(
        self,
        *,
        worktree: Path,
        issue_number: int,
        parent_session_name: str,
        agent_label: str,
    ) -> ReviewExchangeRun:
        retention_days = (
            self._config.session_output_retention_days
            if self._config is not None
            else 7
        )
        retention_tier = (
            self._config.session_output_retention_tier
            if self._config is not None
            else "hot"
        )
        return self._session_output.start_review_exchange_run(
            worktree,
            issue_number=issue_number,
            parent_session_name=parent_session_name,
            agent_label=agent_label,
            retention_days=retention_days,
            retention_tier=retention_tier,
        )

    @staticmethod
    def _require_matching_review_run(
        exchange_result: ReviewExchangeOutcome,
        review_run: ReviewExchangeRun,
    ) -> None:
        if exchange_result.run_assets != review_run.assets:
            raise RuntimeError(
                "review exchange outcome assets do not match allocated run: "
                f"allocated={review_run.assets.run_dir} "
                f"outcome={exchange_result.run_assets.run_dir}"
            )

    def store_review_exchange_summary(
        self,
        *,
        review_run: ReviewExchangeRun,
        exchange_result: ReviewExchangeOutcome,
        current_head_sha: str | None = None,
    ) -> None:
        self._require_matching_review_run(exchange_result, review_run)
        summary = exchange_result.summary
        if summary is not None:
            summary = summary.with_head_sha_if_missing(current_head_sha)
            self._session_output.store_review_exchange_summary(
                review_run,
                summary,
            )
        outcome = (
            summary.status.value
            if summary is not None
            else exchange_result.status.value
        )
        self._finalize_review_run(review_run, outcome=outcome)

    def _execute_terminal_review_run(
        self,
        *,
        review_run: ReviewExchangeRun,
        current_head_sha: str | None,
        execute: Callable[[], ReviewExchangeOutcome],
    ) -> ReviewExchangeOutcome:
        """Run one exchange and finalize its evidence on every exit path."""
        try:
            exchange_result = execute()
            self._require_matching_review_run(exchange_result, review_run)
            self.store_review_exchange_summary(
                review_run=review_run,
                exchange_result=exchange_result,
                current_head_sha=current_head_sha,
            )
        except Exception:
            self._finalize_review_run(review_run, outcome="error")
            raise
        return exchange_result

    def _finalize_background_review_run(self, job_id: str, *, outcome: str) -> None:
        with self._review_runs_lock:
            review_run = self._review_runs_by_job_id.pop(job_id, None)
        if review_run is not None:
            self._finalize_review_run(review_run, outcome=outcome)

    def _finalize_review_run(
        self,
        review_run: ReviewExchangeRun,
        *,
        outcome: str,
    ) -> None:
        self._timeline_evidence.finalize_terminal(
            FinalizeTimelineEvidenceCommand(
                identity=TimelineEvidenceIdentity(
                    review_run.issue_number,
                    review_run.assets.run_dir,
                ),
                outcome=outcome,
            )
        )

    def decide_review_exchange_resumption(
        self,
        worktree: Path,
        session_name: str | None,
        *,
        require_validation: bool,
        current_validation_record_path: Path | None = None,
        current_head_sha: str | None = None,
        not_before_started_at: str | None = None,
    ) -> ResumeResolution:
        resolver = ReviewExchangeCacheResolver(
            session_output=self._session_output,
            validation_head_sha=self._validation_head_sha,
            current_validation_failed=self._current_validation_explicitly_failed,
            cached_validation_passed=self.review_exchange_validation_passed,
        )
        return resolver.decide_review_exchange_resumption(
            worktree,
            session_name,
            require_validation=require_validation,
            current_validation_record_path=current_validation_record_path,
            current_head_sha=current_head_sha,
            not_before_started_at=not_before_started_at,
        )

    @staticmethod
    def review_exchange_validation_passed(record_path: Path | None) -> bool:
        if not record_path or not record_path.exists():
            return False
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return bool(data.get("passed"))

    @staticmethod
    def _current_validation_explicitly_failed(record_path: Path | None) -> bool:
        """Return True iff the record exists and explicitly says ``passed=False``.

        Distinct from ``not review_exchange_validation_passed``: a missing or
        unreadable record is treated as "no signal" (False), so this only
        rejects the cache when the caller produced a definitive failure.
        """
        if not record_path or not record_path.exists():
            return False
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return data.get("passed") is False

    @classmethod
    def _review_exchange_validation_matches_current(
        cls,
        cached_record_path: Path | None,
        current_record_path: Path | None,
    ) -> bool:
        current_sha = cls._validation_head_sha(current_record_path)
        if not current_sha:
            # Strict cache callers reject missing current validation before this
            # helper. Older/dev resume paths keep their prior cache behavior.
            return True
        cached_sha = cls._validation_head_sha(cached_record_path)
        return cached_sha == current_sha

    @staticmethod
    def _validation_head_sha(record_path: Path | None) -> str | None:
        if not record_path or not record_path.exists():
            return None
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        head_sha = data.get("head_sha")
        return head_sha if isinstance(head_sha, str) and head_sha else None

    def resolve_review_exchange_mode(self, agent_label: str | None) -> str | None:
        if not self._config:
            return None
        mode = self._config.review_exchange_mode
        if agent_label:
            configured_reviewer = self._config.get_reviewer_for_agent(agent_label)
            if not configured_reviewer:
                logger.info(
                    "Review exchange disabled for %s: no reviewer configured",
                    agent_label,
                )
                return None
        if mode in {"via-mcp", "via-local-loop"}:
            agent_label = self.require_review_exchange_agent_label(agent_label, mode)
            if mode == "via-mcp":
                from ..infra.review_exchange_registry import supports_mcp_pair

                coder_system, reviewer_system = self.resolve_exchange_systems(
                    agent_label
                )
                if not supports_mcp_pair(coder_system, reviewer_system):
                    raise ValueError(
                        "Review exchange via-mcp requires a supported ai_system pair: "
                        f"{agent_label} ({coder_system}->{reviewer_system})"
                    )
            return mode
        if mode != "auto":
            return None
        if not agent_label:
            logger.warning(
                "Review exchange auto mode requires agent label; falling back to draft PR."
            )
            return None
        agent_label = self.require_review_exchange_agent_label(agent_label, "auto")
        from ..infra.review_exchange_registry import supports_mcp_pair

        coder_system, reviewer_system = self.resolve_exchange_systems(agent_label)
        if supports_mcp_pair(coder_system, reviewer_system):
            return "via-mcp"
        return "via-local-loop"

    def require_review_exchange_agent_label(
        self, agent_label: str | None, mode: str
    ) -> str:
        if not agent_label:
            raise ValueError(f"Review exchange requires agent_label for {mode} mode")
        return agent_label

    def resolve_reviewer_label(self, agent_label: str) -> str:
        if not self._config:
            raise ValueError("Review exchange requires config")
        if agent_label not in self._config.agents:
            raise ValueError(
                f"Review exchange agent '{agent_label}' not found in config.agents"
            )
        reviewer_label = self._config.get_reviewer_for_agent(agent_label)
        if not reviewer_label:
            raise ValueError(
                "Review exchange requires review.default or per-agent reviewer"
            )
        if reviewer_label not in self._config.agents:
            raise ValueError(
                f"Review exchange reviewer '{reviewer_label}' not found in config.agents"
            )
        return reviewer_label

    def resolve_exchange_systems(self, agent_label: str) -> tuple[str, str]:
        if not self._config:
            raise ValueError("Review exchange requires config")
        reviewer_label = self.resolve_reviewer_label(agent_label)
        coder_system = self._config.agents[agent_label].ai_system
        reviewer_system = self._config.agents[reviewer_label].ai_system
        if not coder_system or not reviewer_system:
            raise ValueError(
                "Review exchange requires ai_system on coder and reviewer agents"
            )
        return coder_system, reviewer_system

    def runtime_config_reference(self) -> RuntimeConfigReference:
        if not self._config:
            raise ValueError("Review exchange requires config")
        return self._config.runtime_config_reference()

    def run_review_exchange_loop(
        self,
        *,
        exchange_run: ReviewExchangeRun,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        initial_validation_record_path: Path | None = None,
        approval_gate: "ReviewExchangeApprovalGate | None" = None,
        events: Any | None = None,
        event_context: Any | None = None,
    ) -> Any:
        if not self._config:
            raise ValueError("Review exchange requires config")
        if not agent_label:
            raise ValueError("Review exchange requires agent_label")
        coder_label = agent_label
        reviewer_label = self.resolve_reviewer_label(agent_label)
        coder_agent = self._config.agents[coder_label]
        reviewer_agent = self._config.agents[reviewer_label]
        nit_policy = self._config.review_nits_by_agent.get(
            coder_label,
            self._config.review_nits_default_policy,
        )

        # Reviewer-worktree lifecycle and the round loop live behind the
        # ``ReviewExchangeRunner`` port — ``control/`` no longer reaches
        # into ``execution/`` directly (was done via ``importlib`` in the
        # cutover; replaced by the injected port per #6161).
        return self._review_exchange_runner.run(
            exchange_run=exchange_run,
            coder_worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            coder_label=coder_label,
            reviewer_label=reviewer_label,
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            runtime_config=self.runtime_config_reference(),
            max_rounds=self._config.review_exchange_max_rounds,
            max_no_progress=self._config.review_exchange_max_no_progress,
            require_validation=self._config.review_exchange_require_validation,
            nit_policy=nit_policy,
            initial_validation_record_path=initial_validation_record_path,
            approval_gate=approval_gate,
            # Through the bound-endpoint owner, not the configured
            # value: the engine binds an auto-assigned port and never
            # writes it back into Config, so the raw value is 0 and the
            # exchange exported an undialable port (#6924).
            web_port=self._agent_callback_endpoint.resolve_port(
                self._config.control_api_port
            ),
            events=events,
            event_context=event_context,
        )


def _require_review_exchange_running_query(query: object) -> None:
    if not isinstance(query, ReviewExchangeRunningQuery):
        raise TypeError("query must be a ReviewExchangeRunningQuery")
