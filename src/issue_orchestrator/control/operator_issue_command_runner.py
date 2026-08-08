"""The one implementation of an operator retry/dismiss transition (#6999 F5/A2).

Both commands are the same shape: settle the LABELS first, and only if that
committed, settle the LOCAL STATE. The order is the whole point. Local retry
gates - ``session_history``, ``failed_this_cycle``, the queue cache - are what
stop the planner relaunching into an issue, so clearing them while GitHub still
carries a blocking label makes the planner walk straight back into it. The
reverse mistake is just as bad: an operator told "queued for retry" whose issue
never becomes eligible, because the label came off and the gate did not.

Retry and dismiss differ in exactly two ways, and both are named here rather
than reconstructed by a caller:

* WHICH labels each clears (:class:`~.operator_unblock.OperatorUnblocker`);
* WHAT settling means afterwards - retry makes the issue eligible again and
  refreshes the cached copy, dismiss removes it from the board entirely.

Everything else - the refusal contract, the failure contract, the outcome the
transport maps - is shared, because an operator pressing either button is owed
the same honesty about what did and did not happen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, is_dataclass, replace
from enum import Enum
from typing import Any, TYPE_CHECKING

from .operator_unblock import OperatorUnblockOutcome, OperatorUnblocker
from .queue_cache import QueueCache
from .retry_history_state import RetryHistoryState

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.operator_issue_commands import LockedRunner
    from ..ports.queue_cache_store import QueueCacheStore

logger = logging.getLogger(__name__)


class OperatorCommandStatus(Enum):
    """How far an operator command got. The transport maps only this."""

    #: Labels cleared AND local state settled. The operator's request happened.
    COMMITTED = "committed"
    #: The shared needs-human block is still on the issue - its owner refused,
    #: or the write did not commit. Nothing after it was touched.
    STILL_BLOCKED = "still_blocked"
    #: Ordinary gating labels would not come off GitHub, so local state was
    #: deliberately left in place rather than letting the planner relaunch.
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class OperatorCommandOutcome:
    """One settled operator transition, in terms the transport can map.

    Carries the sentence an operator reads, not a code the transport has to
    turn into one: the two endpoints had drifted into describing the same
    refusal differently, which is the smaller half of the same defect.
    """

    status: OperatorCommandStatus
    issue_number: int
    #: Past participle of the command ("retried", "dismissed").
    performed: str
    removed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    #: The shared block, when it is what stopped the command.
    blocked: str | None = None
    #: Which lifecycles are holding that block, for the operator to act on.
    held_by: tuple[str, ...] = field(default_factory=tuple)
    #: What the operator is told on the committed path.
    message: str = ""

    @property
    def committed(self) -> bool:
        return self.status is OperatorCommandStatus.COMMITTED

    def payload(self) -> dict[str, Any]:
        """The response body, whichever way the command went."""
        if self.status is OperatorCommandStatus.COMMITTED:
            return {
                "success": True,
                "message": self.message,
                "removed_labels": list(self.removed),
            }
        if self.status is OperatorCommandStatus.STILL_BLOCKED:
            cause = (
                f"{', '.join(self.held_by)} still requires it"
                if self.held_by
                else "it could not be cleared"
            )
            return {
                "success": False,
                "error": (
                    f"Issue #{self.issue_number} was not {self.performed}: "
                    f"{self.blocked} is still on the issue because {cause}."
                ),
                "removed_labels": list(self.removed),
                "failed_labels": [self.blocked],
                "held_by": list(self.held_by),
            }
        return {
            "success": False,
            "error": (
                f"Issue #{self.issue_number} was not {self.performed}: failed to "
                f"remove {list(self.failed)} from GitHub. Removed "
                f"{list(self.removed)} successfully; retry the action."
            ),
            "removed_labels": list(self.removed),
            "failed_labels": list(self.failed),
        }


@dataclass(frozen=True, slots=True)
class OperatorIssueCommandRunner:
    """Retry and dismiss, each as one settled transition."""

    unblocker: OperatorUnblocker
    #: Labels as GitHub has them RIGHT NOW. Deliberately the fresh port and not
    #: a cached read: retry decides which labels to strip from this, and a cache
    #: that quietly answered "no labels" would strip nothing, clear the gates
    #: anyway, and hand the planner an issue GitHub still blocks.
    fresh_labels: "FreshIssueReader"
    config: "Config"
    queue_cache_store: "QueueCacheStore"
    state: Callable[[], "OrchestratorState"]
    run_locked: "LockedRunner"

    def retry(self, issue_number: int) -> OperatorCommandOutcome:
        """Clear the retry-gating labels, then make the issue eligible again."""
        current = self.fresh_labels.read_issue_labels(issue_number)
        outcome = self.unblocker.retry(issue_number, current)
        settled = self._refused(issue_number, "retried", outcome)
        if settled is not None:
            return settled
        if outcome.failed:
            # The gates stay ON. Reported as a partial failure so the UI does
            # not show a "queued for retry" toast for an issue GitHub still
            # blocks, and so the operator knows to press it again.
            logger.warning(
                "[retry] Issue #%d retry incomplete: removed=%s, could not "
                "remove=%s; retry gates left in place so the planner will not "
                "relaunch into a still-blocked issue",
                issue_number,
                list(outcome.removed),
                list(outcome.failed),
            )
            return OperatorCommandOutcome(
                status=OperatorCommandStatus.INCOMPLETE,
                issue_number=issue_number,
                performed="retried",
                removed=outcome.removed,
                failed=outcome.failed,
            )
        self.run_locked(lambda: self._make_retryable(issue_number, outcome.removed))
        logger.info(
            "[retry] Issue #%d retried, removed labels: %s",
            issue_number,
            list(outcome.removed),
        )
        return OperatorCommandOutcome(
            status=OperatorCommandStatus.COMMITTED,
            issue_number=issue_number,
            performed="retried",
            removed=outcome.removed,
            message=f"Issue #{issue_number} queued for retry",
        )

    def dismiss(self, issue_number: int) -> OperatorCommandOutcome:
        """Clear everything holding the issue, then take it off the board.

        An ORDINARY label that would not come off stays tolerated here, exactly
        as dismiss has always tolerated it: a label that is already gone raises
        too, and that is not a reason to refuse an operator who is asking for
        the issue to go away. Only the shared block can stop this command.
        """
        outcome = self.unblocker.dismiss(issue_number)
        settled = self._refused(issue_number, "dismissed", outcome)
        if settled is not None:
            return settled
        self.run_locked(lambda: self._remove_from_board(issue_number))
        logger.info(
            "[dismiss] Issue #%d dismissed, removed labels: %s",
            issue_number,
            list(outcome.removed),
        )
        return OperatorCommandOutcome(
            status=OperatorCommandStatus.COMMITTED,
            issue_number=issue_number,
            performed="dismissed",
            removed=outcome.removed,
            message=f"Issue #{issue_number} dismissed",
        )

    # -- internals ---------------------------------------------------------

    def _refused(
        self, issue_number: int, performed: str, outcome: OperatorUnblockOutcome
    ) -> OperatorCommandOutcome | None:
        """The one refusal both commands share: the shared block stayed on.

        Local state is NOT touched. That is the whole contract - dismiss used
        to prune it anyway and report success, leaving the issue blocked on
        GitHub, invisible locally, and an operator told it was done.
        """
        if outcome.blocked is None:
            return None
        return OperatorCommandOutcome(
            status=OperatorCommandStatus.STILL_BLOCKED,
            issue_number=issue_number,
            performed=performed,
            removed=outcome.removed,
            blocked=outcome.blocked,
            held_by=outcome.held_by,
        )

    def _make_retryable(self, issue_number: int, removed: tuple[str, ...]) -> None:
        """Clear the retry gates, then refresh the cached copy behind them.

        Removing the GitHub label is not enough: ``QueueCache.evaluate_issue``
        rejects any issue whose number is in ``session_history`` (or
        ``failed_this_cycle``), so the planner keeps skipping it on every
        refresh until the orchestrator restarts.
        """
        state = self.state()
        RetryHistoryState(state).make_retryable(issue_number)

        # The cached scope copy still carries the labels just removed; the queue
        # copy will have been rejected after the timeout, so re-evaluate the
        # scope copy against the freshly pruned state.
        cached = next(
            (
                issue for issue in state.cached_scope_issues
                if issue.number == issue_number
            ),
            None,
        )
        if cached is None or not is_dataclass(cached) or isinstance(cached, type):
            return
        updated = replace(
            cached,
            labels=tuple(
                label for label in cached.labels if label not in removed
            ),
        )
        queue_cache = QueueCache(self.config, state, self.queue_cache_store)
        queue_cache.upsert_refreshed_issue(updated)
        queue_cache.save_snapshot()
        logger.debug(
            "[cache] Reset issue #%d for retry: removed labels=%s",
            issue_number,
            list(removed),
        )

    def _remove_from_board(self, issue_number: int) -> None:
        state = self.state()
        state.session_history = [
            entry for entry in state.session_history
            if entry.issue_number != issue_number
        ]
        QueueCache(self.config, state, self.queue_cache_store).remove_issue_and_save(
            issue_number
        )


__all__ = [
    "OperatorCommandOutcome",
    "OperatorCommandStatus",
    "OperatorIssueCommandRunner",
]
