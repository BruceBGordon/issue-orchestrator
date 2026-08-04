"""Owner for manual publish recovery of publish-failed issues.

The dashboard "Retry publish" button lands here. A publish failure means a
completed coding session pushed/created-PR unsuccessfully; the work itself is
intact in the worktree (branch + completion record on disk). This service:

1. Records durable retry *locators* when a publish fails (so the issue stays
   retryable across restarts) via :class:`PublishRetryLocatorStore`.
2. On retry, either recovers an already-created PR, or re-runs the publish
   **off the request thread** on the shared :class:`BackgroundJobRunner` — the
   same live-completion runner introduced by #6573 — reconstructing the inputs
   from the stored locators plus the on-disk completion record.
3. Reconciles success on the next ``tick`` drain: clears publish-failed state
   and the stored locators.

The heavy work (``CompletionProcessor.process``: validate — cache-aware on the
same commit — + push + PR) must stay off the request thread, which is why the
republish is dispatched to the runner rather than run inline.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol

from ..control.actions import (
    AddLabelAction,
    RemoveLabelAction,
    SupersedePullRequestAction,
)
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from ..domain.models import OrchestratorState
from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from ..domain.publish_retry import PublishRetryLocators
from ..ports.background_job import BackgroundJobRunner, CompletedJob
from ..ports.fresh_issue_reader import FreshIssueReadError, FreshIssueReader
from ..ports.publish_retry_locator_store import PublishRetryLocatorStore
from ..ports.pull_request_tracker import PRInfo
from ..ports.tech_lead_authority import TechLeadAuthorityStore
from .completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
    ERROR_PREFIX_PUSH,
)
from .publish_retry_admission import (
    board_block_reason,
    locator_block_reason,
    restore_completion_record,
)
from .publish_retry_drain import classify_drained_retry
from .publish_retry_finalize import RetryReviewRouting, RetrySuccessFinalizer
from .republish_job_id import RepublishJobId

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..domain.session_run import SessionRunAssets
    from .completion_types import ProcessingResult
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

_PUBLISH_FAILURE_PREFIXES = (
    ERROR_PREFIX_PUSH,
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
)


class _RepositoryHost(Protocol):
    def get_issue(self, issue_number: int) -> Any: ...
    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]: ...


class _CompletionProcessor(Protocol):
    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: "SessionRunAssets",
        pr_number: int | None = ...,
        completion_path: str | None = ...,
        agent_label: str | None = ...,
    ) -> "ProcessingResult": ...


class _ActionApplier(Protocol):
    def apply(
        self,
        action: AddLabelAction | RemoveLabelAction | SupersedePullRequestAction,
    ) -> Any: ...


@dataclass(frozen=True)
class RetryPublishResult:
    """Result of a retry-publish request."""

    status: str
    message: str
    job_id: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None


@dataclass(frozen=True)
class _RetryDecision:
    allowed: bool
    reason: str
    locators: PublishRetryLocators | None = None
    issue_title: str = ""
    agent_label: str | None = None


@dataclass(frozen=True)
class _RepublishContext:
    """Reconcile context for an in-flight republish job, kept until it drains.

    ``token`` is a per-submission id owned by :class:`PublishRecoveryService`
    (not the worker thread's alive bit). The worker records its result under the
    same token, and drain correlates the completed job back to this exact
    submission before clearing owner state — so a fast completion can't be lost
    and a stale completion can't be reconciled against a newer submission.
    """

    token: int
    issue_number: int
    issue_title: str
    agent_label: str | None
    worktree_path: str
    branch_name: str
    skip_review: bool


def is_publish_failure(processing_errors: Sequence[str] | None) -> bool:
    """True iff the completion errors indicate a push / PR-creation failure."""
    if not processing_errors:
        return False
    return any(
        str(error).split(":", 1)[0].strip() in _PUBLISH_FAILURE_PREFIXES
        for error in processing_errors
    )


class PublishRecoveryService:
    """Backend owner for retrying or recovering publish-failed issues."""

    def __init__(
        self,
        repository_host: _RepositoryHost,
        completion_processor: _CompletionProcessor,
        locator_store: PublishRetryLocatorStore,
        runner: BackgroundJobRunner,
        label_manager: "LabelManager",
        fresh_issue_reader: FreshIssueReader,
        action_applier: _ActionApplier,
        code_review_agent_configured: bool,
        tech_lead_authority: TechLeadAuthorityStore,
    ) -> None:
        self._repository_host = repository_host
        self._completion_processor = completion_processor
        self._locator_store = locator_store
        self._tech_lead_authority = tech_lead_authority
        self._runner = runner
        self._lm = label_manager
        self._fresh_issue_reader = fresh_issue_reader
        self._action_applier = action_applier
        # A successful retry that produces a PR must clear publish-failed state
        # and route the PR through the same review-discovery policy live
        # completion uses, so a retry-published PR cannot bypass the review gate
        # (F1). That whole finalization is owned by RetrySuccessFinalizer.
        self._finalizer = RetrySuccessFinalizer(
            label_manager=label_manager,
            fresh_issue_reader=fresh_issue_reader,
            action_applier=action_applier,
            code_review_agent_configured=code_review_agent_configured,
        )
        self._lock = Lock()
        # Owner-authoritative in-flight state, guarded by ``self._lock`` and
        # independent of the worker thread's alive bit. ``_pending`` maps an
        # issue to its live submission (recorded BEFORE the worker can start, so
        # a fast completion is never lost); it is only removed when that
        # submission's job is drained, so a completed-but-undrained job still
        # blocks a duplicate retry. ``_results`` is keyed by submission token so
        # a stale completion can't clobber a newer submission's result.
        self._pending: dict[int, _RepublishContext] = {}
        self._results: dict[int, "ProcessingResult"] = {}
        self._token_seq: int = 0
        # Submission tokens whose republish was abandoned (reset/termination)
        # while the worker thread could not be force-killed. Correlation is by
        # token, not issue: an abandoned submission A and a later submission B
        # for the same issue have distinct tokens and distinct runner job ids,
        # so draining A's late completion supersedes A's PR without disturbing
        # B's pending slot. Consumed on the next drain of that token's job.
        self._tombstoned: set[int] = set()

    # ------------------------------------------------------------------
    # Recording (called on the live completion path when a publish fails)
    # ------------------------------------------------------------------

    def record_publish_failure(
        self,
        session: "Session",
        processing_errors: Sequence[str] | None,
        *,
        review_exchange_completed: bool = False,
        review_exchange_halted: bool = False,
    ) -> None:
        """Persist durable retry locators when a session's publish fails.

        Idempotent per issue: overwrites any prior locators. No-op when the
        completion errors are not a publish (push/PR) failure. The original
        completion's review-exchange state is captured so an existing-PR recovery
        (which runs no fresh completion) honors the same review-routing policy as
        live completion.
        """
        if not is_publish_failure(processing_errors):
            return
        locators = PublishRetryLocators(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_key=session.key.stable_id(),
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            completion_path=session.completion_path,
            run_assets=session.run_assets,
            agent_label=session.agent_label,
            pr_number=session.pr_number,
            skip_review=session.agent_config.skip_review,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
        )
        self._locator_store.save(locators)
        logger.info(
            "[publish-retry] Persisted retry locators for issue=%s branch=%s",
            session.issue.number,
            session.branch_name,
        )

    # ------------------------------------------------------------------
    # Retry request (called from the web thread)
    # ------------------------------------------------------------------

    def can_retry_publish(self, issue_number: int, state: OrchestratorState) -> bool:
        """Return whether retry-publish is currently available for an issue."""
        return self._retry_decision(issue_number, state).allowed

    def retry_publish(self, issue_number: int, state: OrchestratorState) -> RetryPublishResult:
        """Retry publish for a publish-failed issue or recover an already-created PR."""
        decision = self._retry_decision(issue_number, state)
        if not decision.allowed or decision.locators is None:
            logger.info(
                "[publish-retry] Rejecting retry request for issue=%s reason=%s",
                issue_number,
                decision.reason,
            )
            return RetryPublishResult(status="rejected", message=decision.reason)

        locators = decision.locators
        existing_pr = self._matching_open_pr(issue_number, locators.branch_name)
        if existing_pr is not None:
            try:
                self._finalizer.finalize(
                    state=state,
                    issue_number=issue_number,
                    issue_title=decision.issue_title,
                    agent_label=decision.agent_label,
                    pr_url=existing_pr.url,
                    pr_number=existing_pr.number,
                    worktree_path=locators.worktree_path,
                    history_reason="Recovered awaiting-merge state from existing PR",
                    routing=RetryReviewRouting(
                        branch_name=locators.branch_name,
                        skip_review=locators.skip_review,
                        # Recovering an already-open PR runs no fresh completion,
                        # so honor the ORIGINAL completion's review-exchange
                        # outcome: if it already completed/halted a local review
                        # exchange, the PR must not be requeued for review.
                        review_exchange_completed=locators.review_exchange_completed,
                        review_exchange_halted=locators.review_exchange_halted,
                    ),
                )
            except FreshIssueReadError as exc:
                # Finalizing composes label cleanup from CURRENT labels; it
                # reads them first and applies nothing before that read, so an
                # unreadable issue leaves the retry state exactly as it was
                # (#6957 round-2 review F4). Report it instead of 500ing the
                # operator's retry request.
                logger.warning(
                    "[publish-retry] Could not finalize recovered PR for issue=%s: %s",
                    issue_number,
                    exc,
                )
                return RetryPublishResult(
                    status="rejected",
                    message=(
                        f"Could not read current labels for issue #{issue_number};"
                        " the issue is unchanged and still retryable"
                    ),
                )
            self._clear_retry_terminal_state(issue_number)
            logger.info(
                "[publish-retry] Recovered existing PR for issue=%s pr=%s branch=%s",
                issue_number,
                existing_pr.number,
                existing_pr.branch,
            )
            return RetryPublishResult(
                status="recovered_existing_pr",
                message=f"Recovered existing PR #{existing_pr.number}",
                pr_url=existing_pr.url,
                pr_number=existing_pr.number,
            )

        job_id = self._submit_republish(
            locators,
            issue_title=decision.issue_title,
            agent_label=decision.agent_label,
        )
        if job_id is None:
            message = "A publish job is already active for this issue"
            logger.info(
                "[publish-retry] Skipping duplicate retry submit for issue=%s",
                issue_number,
            )
            return RetryPublishResult(status="rejected", message=message)

        logger.info(
            "[publish-retry] Submitted publish retry for issue=%s branch=%s",
            issue_number,
            locators.branch_name,
        )
        return RetryPublishResult(
            status="submitted",
            message="Publish retry queued",
            job_id=job_id,
        )

    # ------------------------------------------------------------------
    # Termination (called when an issue's attempt is reset / torn down)
    # ------------------------------------------------------------------

    def has_active_retry(self, issue_number: int) -> bool:
        """Report whether a publish retry is in-flight or stored for the issue.

        Non-mutating counterpart to :meth:`abandon_issue`: it reads the exact
        state ``abandon_issue`` would clear — the live submission in ``_pending``
        and the durable locators — so a lifecycle boundary that must decide
        whether resetting would abandon live publish-retry work reads the same
        owner state the abandon mutates, and the two can never drift.
        """
        with self._lock:
            pending_present = issue_number in self._pending
        return pending_present or self._locator_store.get(issue_number) is not None

    def abandon_issue(self, issue_number: int) -> None:
        """Abandon any in-flight publish retry and drop the durable locators.

        Reset (and any issue-runtime teardown) calls this so a republish worker
        that finishes *after* the attempt is discarded cannot repopulate the
        superseded attempt — removing ``publish-failed``, adding ``pr-pending``,
        appending completed history, or leaving an unsuperseded stale PR.

        The daemon worker thread cannot be force-killed mid-``process``, so this
        does the honest thing instead of pretending to cancel it:

        - drops the in-flight republish context/result so a late drain is
          ignored rather than reconciled as success,
        - clears the stored locators (this attempt is no longer retryable),
        - tombstones the issue so :meth:`drain_completed_retries` supersedes any
          PR the late worker created before it was abandoned.

        The tombstone is scoped to the single in-flight submission (by token):
        it is consumed by the next drain of that job (which fires within a tick,
        long before any fresh attempt could fail publish and submit a new
        republish), so it never drops a legitimate later retry.
        """
        with self._lock:
            context = self._pending.pop(issue_number, None)
            if context is not None:
                # Drop the in-flight result and tombstone this exact submission
                # (by token) so its late completion is superseded, not
                # reconciled — and a fresh submission for the same issue is
                # unaffected.
                self._results.pop(context.token, None)
                self._tombstoned.add(context.token)
                logger.info(
                    "[publish-retry] Abandoned in-flight republish for issue=%s "
                    "token=%s; late completion will be superseded",
                    issue_number,
                    context.token,
                )
        self._clear_retry_terminal_state(issue_number)

    def _clear_retry_terminal_state(self, issue_number: int) -> None:
        """Drop the locators AND both of the run's tech_lead ledger rows.

        A publish-retryable failure keeps the authority row alive so the
        retry's re-entry into ``CompletionProcessor.process`` can re-validate
        the launch scope; once the retry reaches its own terminal (success
        finalization, existing-PR recovery, or abandonment) the run is truly
        over and the rows must not outlive it (#6769 F3). This is the
        publish-failure counterpart of
        :func:`discard_tech_lead_authority_after_completion` — skipped on exactly
        this path, so it owes the same PAIR of releases: dropping only the
        run-keyed row orphans a storm anchor's cohort row (#6780). Both
        ``discard`` calls are no-ops for non-tech-lead runs.
        """
        locators = self._locator_store.get(issue_number)
        if locators is not None:
            self._tech_lead_authority.discard(
                run_id=locators.run_assets.run_id,
                session_name=locators.run_assets.session_name,
            )
        self._tech_lead_authority.discard_storm_cohort(anchor_issue_number=issue_number)
        self._locator_store.clear(issue_number)

    # ------------------------------------------------------------------
    # Reconciliation (drained on the tick thread)
    # ------------------------------------------------------------------

    def drain_completed_retries(self, state: OrchestratorState) -> None:
        """Reconcile republish jobs that finished since the last tick.

        On success: clear publish-failed state + stored locators. On failure or
        error: leave the publish-failed label and locators in place so the issue
        stays retryable (no permanent lockout). Jobs whose issue was abandoned
        (reset/termination) are superseded, not reconciled.
        """
        for job in self._runner.drain_completed():
            parsed = RepublishJobId.parse(job.job_id)
            if parsed is None:
                continue
            issue_number, token = parsed.issue_number, parsed.token
            with self._lock:
                # Correlate strictly by submission token. Only remove the
                # issue's pending slot when THIS completion is the one it holds,
                # so draining an old abandoned submission cannot evict a newer
                # submission for the same issue (F2).
                result = self._results.pop(token, None)
                tombstoned = token in self._tombstoned
                self._tombstoned.discard(token)
                context = self._pending.get(issue_number)
                if context is not None and context.token == token:
                    self._pending.pop(issue_number, None)
                else:
                    context = None
            if tombstoned:
                self._supersede_abandoned_retry(issue_number, job, result)
                continue
            if context is None:
                # A stale/abandoned/already-drained submission — ignore. (The
                # newer submission stays pending and reconciles on its own drain.)
                continue
            outcome = classify_drained_retry(job_error=job.error, result=result)
            if not outcome.may_finalize:
                logger.log(
                    logging.ERROR if outcome.faulted else logging.INFO,
                    "[publish-retry] Issue=%s stays retryable: %s",
                    issue_number,
                    outcome.reason,
                )
                continue
            assert result is not None  # only a successful result may finalize
            try:
                self.reconcile_retry_publish_success(
                    state=state,
                    issue_number=issue_number,
                    issue_title=context.issue_title,
                    agent_label=context.agent_label,
                    pr_url=result.pr_url,
                    pr_number=self._extract_pr_number(result.pr_url),
                    worktree_path=context.worktree_path,
                    review_routing=RetryReviewRouting(
                        branch_name=context.branch_name,
                        skip_review=context.skip_review,
                        review_exchange_completed=result.review_exchange_completed,
                        review_exchange_halted=result.review_exchange_halted,
                    ),
                )
            except FreshIssueReadError as exc:
                # This drain runs on the tick thread. Finalizing reads current
                # labels before it applies anything, so an unreadable issue
                # leaves the retry state intact — same shape as the other
                # "leaving issue retryable" branches above, and it must not
                # abort the remaining jobs in this drain (#6957 R2 F4).
                logger.warning(
                    "[publish-retry] Could not finalize republish for issue=%s;"
                    " leaving it retryable: %s",
                    issue_number,
                    exc,
                )
                continue
            self._clear_retry_terminal_state(issue_number)

    def reconcile_retry_publish_success(
        self,
        *,
        state: OrchestratorState,
        issue_number: int,
        issue_title: str,
        agent_label: str | None,
        pr_url: str | None,
        pr_number: int | None,
        worktree_path: str | None,
        review_routing: RetryReviewRouting | None = None,
    ) -> None:
        """Clear stale publish-failed state after a manual publish retry succeeds.

        ``review_routing`` carries the session-level inputs needed to decide
        whether the (re)published PR still needs the configured code review. It
        defaults to a review-neutral routing so callers that only want the label
        cleanup keep their prior behavior.
        """
        self._finalizer.finalize(
            state=state,
            issue_number=issue_number,
            issue_title=issue_title,
            agent_label=agent_label,
            pr_url=pr_url,
            pr_number=pr_number,
            worktree_path=worktree_path,
            history_reason="Publish retry succeeded",
            routing=review_routing
            or RetryReviewRouting(
                branch_name="",
                skip_review=False,
                review_exchange_completed=False,
                review_exchange_halted=False,
            ),
        )
        logger.info(
            "[publish-retry] Finalized successful retry for issue=%s pr=%s",
            issue_number,
            pr_number,
        )

    def _supersede_abandoned_retry(
        self,
        issue_number: int,
        job: CompletedJob,
        result: "ProcessingResult | None",
    ) -> None:
        """Close any PR left open by a republish that finished after abandon.

        No labels/history are touched — the attempt was discarded by reset.
        Only a PR the late worker created past reset's supersede scan needs
        closing, and only while it is still open (reset may have already
        superseded it if the push landed before the scan).
        """
        if job.error is not None:
            logger.warning(
                "[publish-retry] Abandoned republish for issue=%s raised after "
                "reset: %s",
                issue_number,
                job.error,
            )
            return
        pr_number = self._extract_pr_number(result.pr_url) if result else None
        if result is None or not result.success or pr_number is None:
            logger.info(
                "[publish-retry] Abandoned republish for issue=%s produced no "
                "PR to supersede",
                issue_number,
            )
            return
        open_prs = self._repository_host.get_prs_for_issue(issue_number, state="open")
        if not any(pr.number == pr_number for pr in open_prs):
            logger.info(
                "[publish-retry] Late retry PR #%s for reset issue=%s already "
                "closed/superseded",
                pr_number,
                issue_number,
            )
            return
        try:
            outcome = self._action_applier.apply(
                SupersedePullRequestAction(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    comment=(
                        "Superseded: this PR was created by a publish retry that "
                        "finished after the issue was reset. The orchestrator has "
                        "discarded that attempt; a fresh attempt will publish "
                        "separately."
                    ),
                    reason="publish retry completed after reset",
                )
            )
        except (ClaimLostError, ReconciliationRequired) as exc:
            # ActionApplier re-raises these on a claim/state race (e.g. a fresh
            # attempt already re-claimed the issue). This drain runs on the tick
            # thread, so swallowing them keeps a reset/fresh-attempt race from
            # aborting the whole tick after the tombstone was already consumed.
            # The late PR is left open (stranded) rather than force-closed.
            logger.warning(
                "[publish-retry] Left late retry PR #%s for issue=%s open after "
                "a claim/reconciliation race: %s",
                pr_number,
                issue_number,
                exc,
            )
            return
        if not outcome.success:
            logger.error(
                "[publish-retry] Failed to supersede late retry PR #%s for "
                "issue=%s: %s",
                pr_number,
                issue_number,
                outcome.error,
            )
            return
        logger.info(
            "[publish-retry] Superseded late retry PR #%s for reset issue=%s",
            pr_number,
            issue_number,
        )

    # ------------------------------------------------------------------
    # Decision / gating
    # ------------------------------------------------------------------

    def _retry_decision(self, issue_number: int, state: OrchestratorState) -> _RetryDecision:
        issue = self._repository_host.get_issue(issue_number)
        if issue is None:
            return _RetryDecision(False, f"Issue #{issue_number} not found")

        try:
            labels = tuple(self._current_labels(issue_number))
        except FreshIssueReadError as exc:
            # The gate below is a correctness check against CURRENT labels, so
            # an unreadable issue is "cannot decide", not "no blocking labels"
            # (#6957 round-2 review F4). Rejecting keeps the retry available:
            # the operator can retry once GitHub answers again.
            logger.warning(
                "[publish-retry] Cannot decide retry for issue=%s: %s",
                issue_number,
                exc,
            )
            return _RetryDecision(
                False, f"Could not read current labels for issue #{issue_number}"
            )
        block_reason = self._retry_state_block_reason(issue_number, state, labels)
        if block_reason:
            return _RetryDecision(False, block_reason)

        locators = self._locator_store.get(issue_number)
        if locators is None:
            return _RetryDecision(False, "No publish-retry locators found for issue")

        locator_reason = locator_block_reason(locators)
        if locator_reason:
            return _RetryDecision(False, locator_reason)

        issue_title = str(getattr(issue, "title", "") or locators.issue_title or f"Issue #{issue_number}")
        agent_label = self._resolve_agent_label(issue, locators)
        return _RetryDecision(
            True,
            "ok",
            locators=locators,
            issue_title=issue_title,
            agent_label=agent_label,
        )

    def _retry_state_block_reason(
        self,
        issue_number: int,
        state: OrchestratorState,
        labels: tuple[str, ...],
    ) -> str | None:
        board_reason = board_block_reason(
            issue_number=issue_number,
            state=state,
            labels=labels,
            publish_failed_label=self._lm.publish_failed,
        )
        if board_reason:
            return board_reason
        # Owner state, not the worker's alive bit: a submission stays "pending"
        # until its job is drained, so a completed-but-undrained retry still
        # blocks a duplicate. The authoritative gate is the atomic reserve in
        # ``_submit_republish``; this only supplies an early, friendlier reason.
        with self._lock:
            if issue_number in self._pending:
                return "Issue already has a pending publish retry"
        return None

    # ------------------------------------------------------------------
    # Republish submission
    # ------------------------------------------------------------------

    def _submit_republish(
        self,
        locators: PublishRetryLocators,
        *,
        issue_title: str,
        agent_label: str | None,
    ) -> str | None:
        """Submit a republish; return its submission-scoped job id, or None if a
        prior submission for the issue is still pending."""
        issue_number = locators.issue_number

        # Reserve the in-flight slot BEFORE the worker can start, so a fast
        # completion that drains before submit() returns still has an owner
        # context to reconcile against. Reject atomically if a prior submission
        # is still pending (in-flight or completed-but-undrained).
        with self._lock:
            if issue_number in self._pending:
                return None
            token = self._token_seq
            self._token_seq += 1
            self._pending[issue_number] = _RepublishContext(
                token=token,
                issue_number=issue_number,
                issue_title=issue_title,
                agent_label=agent_label,
                worktree_path=locators.worktree_path,
                branch_name=locators.branch_name,
                skip_review=locators.skip_review,
            )
        job_id = RepublishJobId(issue_number, token).encode()

        def run() -> None:
            restore_completion_record(locators)
            result = self._completion_processor.process(
                Path(locators.worktree_path),
                issue_number,
                issue_title,
                run_assets=locators.run_assets,
                pr_number=locators.pr_number,
                completion_path=locators.completion_path,
                agent_label=agent_label,
            )
            with self._lock:
                self._results[token] = result

        if not self._runner.submit(job_id, run):
            # The owner gate above already guarantees no live job for this issue,
            # so this is defensive: undo the reservation to keep state consistent.
            with self._lock:
                self._pending.pop(issue_number, None)
            return None
        return job_id

    @staticmethod
    def _extract_pr_number(pr_url: str | None) -> int | None:
        if not pr_url:
            return None
        parts = pr_url.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "pull":
            try:
                return int(parts[-1])
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    # Success finalization + labels
    # ------------------------------------------------------------------

    def _matching_open_pr(self, issue_number: int, expected_branch: str) -> PRInfo | None:
        prs = self._repository_host.get_prs_for_issue(issue_number, state="open")
        scoped = scope_prs_to_active_issue_branch(
            issue_number,
            prs,
            expected_branch=expected_branch,
        )
        if scoped.ignored:
            logger.info(
                "[publish-retry] Ignoring %d prior-attempt PR(s) for issue=%s expected_branch=%s",
                len(scoped.ignored),
                issue_number,
                expected_branch,
            )
        return scoped.first_matching

    def _current_labels(self, issue_number: int) -> list[str]:
        return [str(label) for label in self._fresh_issue_reader.read_issue_labels(issue_number)]

    def _resolve_agent_label(self, issue: Any, locators: PublishRetryLocators) -> str | None:
        for label in getattr(issue, "labels", ()) or ():
            if str(label).startswith("agent:"):
                return str(label)
        if locators.agent_label and locators.agent_label.strip():
            return locators.agent_label
        return None
