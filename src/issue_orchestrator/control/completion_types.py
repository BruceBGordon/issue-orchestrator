"""Shared completion-processing result types."""

from dataclasses import dataclass

ERROR_PREFIX_PUSH = "push_branch"
ERROR_PREFIX_CREATE_PR = "create_pr"
ERROR_PREFIX_PUBLISH_BLOCKED = "publish_blocked"
# A COMPLETED tech_lead session whose decision artifact pair is missing or
# rejected. Classified critical so the session's authoritative outcome is
# FAILED (ADR-0031 / #6761 finding 3), not a quiet success.
ERROR_PREFIX_TECH_LEAD_DECISION = "tech_lead_decision"
# A COMPLETED tech_lead session whose orchestrator-owned launch authority is
# missing, or whose agent-writable worktree copies (assignment / manifest)
# no longer match it — tamper evidence. Critical like the decision prefix
# (#6761 re-review finding 1).
ERROR_PREFIX_TECH_LEAD_AUTHORITY = "tech_lead_authority"
# A label that only NeedsHumanBlock may write reached an ordinary label writer
# (#6999 F2 round 5). Its own prefix, and NOT one of the publish prefixes,
# because the two consequences differ: a publish error is retried by the
# publish-recovery lane, whereas re-running this one would refuse again. What it
# must do instead is force the completion to FAIL, so an untrusted block request
# that was dropped can never be reported as a success.
ERROR_PREFIX_GOVERNED_LABEL = "governed_label"
REVIEW_EXCHANGE_ERROR_PREFIX = "review_exchange:"


@dataclass
class ProcessingResult:
    """Result of processing a completion record."""

    success: bool
    message: str
    failure_kind: str | None = None
    pr_url: str | None = None
    actions_taken: list[str] | None = None
    diagnostic_path: str | None = None
    completion_record_path: str | None = None
    errors: list[str] | None = None
    review_exchange_completed: bool = False
    review_exchange_halted: bool = False
    # True when the review exchange is running asynchronously and completion
    # processing for this record must retry on a future tick. Callers must NOT
    # treat the session as terminated while this flag is set — the completion
    # record is intentionally left on disk so the next observation re-enters
    # the pipeline.
    review_exchange_deferred: bool = False
    # True when a post-review validation failure was preserved and rerouted
    # back into coder rework via the review-exchange path. Callers should keep
    # the session running but still surface validation-failure evidence.
    validation_failed_rerouted: bool = False

    @classmethod
    def for_review_exchange_deferred(cls) -> "ProcessingResult":
        """Typed constructor for the async review-exchange deferral result."""
        return cls(
            success=True,
            message="Review exchange running in background; will resume on next tick",
            completion_record_path=None,
            review_exchange_deferred=True,
        )

    @property
    def is_non_terminal(self) -> bool:
        """True when completion has NOT finished for this record.

        The review exchange is running in the background (``review_exchange_deferred``)
        and/or a post-review validation failure was rerouted into coder rework
        (``validation_failed_rerouted``). The live session path leaves such a
        completion pending — ``SessionController`` maps it to ``SessionStatus.RUNNING``
        and resumes publishing on a later tick. Other consumers of a
        ``ProcessingResult`` (e.g. retry-publish reconciliation) must not treat a
        non-terminal result as terminal success, or they would clear recovery
        state before publish actually completes.
        """
        return self.review_exchange_deferred or self.validation_failed_rerouted
