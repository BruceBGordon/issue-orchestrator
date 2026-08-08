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

Everything between those two - what counts as committed, what a refusal means,
what a failed write means - runs through ONE method, because the last time the
two paths spelled it out separately dismiss quietly lost a branch of it twice:
first the shared-block refusal (#6999 F3), then the failed ordinary write
(#6999 F5 round 7). A difference that is not one of the two named above is a
bug, so there is no longer anywhere to write one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, is_dataclass, replace
from typing import TYPE_CHECKING

from ..ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandOutcome,
    OperatorCommandStatus,
)
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
        observed = tuple(self.fresh_labels.read_issue_labels(issue_number))
        return self._settle(
            issue_number,
            OperatorCommandIntent.RETRY,
            self.unblocker.retry(issue_number, observed),
            lambda removed: self._make_retryable(issue_number, observed, removed),
        )

    def dismiss(self, issue_number: int) -> OperatorCommandOutcome:
        """Clear everything holding the issue, then take it off the board."""
        return self._settle(
            issue_number,
            OperatorCommandIntent.DISMISS,
            self.unblocker.dismiss(issue_number),
            lambda removed: self._remove_from_board(issue_number),
        )

    # -- internals ---------------------------------------------------------

    def _settle(
        self,
        issue_number: int,
        intent: OperatorCommandIntent,
        labels: OperatorUnblockOutcome,
        commit: Callable[[tuple[str, ...]], None],
    ) -> OperatorCommandOutcome:
        """Apply the ordering invariant, for whichever command asked.

        Two ways the GitHub side can fail to settle, and neither may reach
        ``commit``:

        * the SHARED BLOCK is still on the issue - its owner refused because a
          quarantine or tech-lead escalation needs it, or the write did not
          land. Nothing after it was touched, deliberately: stripping the
          tech-lead marker in the same pass is what once left the block
          standing with nothing to explain or recover it;
        * an ORDINARY gating label would not come off. This is a genuine failed
          write, not a label that was already gone - the repository adapter
          treats a 404 as idempotent success and retries transport faults
          itself, so an exception surfacing here means GitHub still carries the
          label (#6999 F5 round 7). Pruning local state over it would hide an
          issue the board still blocks and, on the retry path, hand the planner
          an issue it relaunches straight into.
        """
        if labels.blocked is not None:
            return self._outcome(
                issue_number,
                intent,
                OperatorCommandStatus.STILL_BLOCKED,
                labels,
            )
        if labels.failed:
            logger.warning(
                "[%s] Issue #%d not settled: removed=%s, GitHub would not "
                "remove=%s; local queue/history state left in place so it "
                "cannot disagree with the board",
                intent.value,
                issue_number,
                list(labels.removed),
                list(labels.failed),
            )
            return self._outcome(
                issue_number, intent, OperatorCommandStatus.INCOMPLETE, labels
            )
        self.run_locked(lambda: commit(labels.removed))
        logger.info(
            "[%s] Issue #%d settled, removed labels: %s",
            intent.value,
            issue_number,
            list(labels.removed),
        )
        return self._outcome(
            issue_number, intent, OperatorCommandStatus.COMMITTED, labels
        )

    def _outcome(
        self,
        issue_number: int,
        intent: OperatorCommandIntent,
        status: OperatorCommandStatus,
        labels: OperatorUnblockOutcome,
    ) -> OperatorCommandOutcome:
        return OperatorCommandOutcome(
            intent=intent,
            status=status,
            issue_number=issue_number,
            removed=labels.removed,
            failed=labels.failed,
            blocked=labels.blocked,
            held_by=labels.held_by,
        )

    def _make_retryable(
        self,
        issue_number: int,
        observed: tuple[str, ...],
        removed: tuple[str, ...],
    ) -> None:
        """Clear the retry gates, then reconcile the cached copy behind them.

        Removing the GitHub label is not enough: ``QueueCache.evaluate_issue``
        rejects any issue whose number is in ``session_history`` (or
        ``failed_this_cycle``), so the planner keeps skipping it on every
        refresh until the orchestrator restarts. And clearing those gates is not
        enough either, because ``Scheduler`` re-reads the cached issue's LABELS
        and refuses anything still wearing a blocking one.

        So the cached copy is reconciled against what GitHub actually had, not
        patched with this attempt's removals (#6999 F7 round 8). The difference
        only shows up across two attempts, which is exactly what the new partial
        failure path made reachable:

        1. the cache and GitHub both carry ``blocked`` and ``blocked-failed``;
        2. a first retry removes ``blocked``, fails on ``blocked-failed``, and
           correctly leaves the cache alone;
        3. the operator retries. The fresh read now shows only
           ``blocked-failed``, so that is all this attempt removes.

        Subtracting only step 3's removals from the step-1 cache leaves
        ``blocked`` on the cached copy - already gone from GitHub, still gating
        the planner - and the operator is told the issue was queued. Starting
        from ``observed``, the pre-write snapshot this attempt actually acted
        on, cannot drift that way: it is authoritative for every label,
        including the non-gating ones the cache would otherwise be trusted for.
        """
        state = self.state()
        RetryHistoryState(state).make_retryable(issue_number)

        cached = next(
            (
                issue for issue in state.cached_scope_issues
                if issue.number == issue_number
            ),
            None,
        )
        if cached is None or not is_dataclass(cached) or isinstance(cached, type):
            return
        settled = tuple(label for label in observed if label not in removed)
        updated = replace(cached, labels=settled)
        queue_cache = QueueCache(self.config, state, self.queue_cache_store)
        queue_cache.upsert_refreshed_issue(updated)
        queue_cache.save_snapshot()
        logger.debug(
            "[cache] Reset issue #%d for retry: removed=%s, settled labels=%s",
            issue_number,
            list(removed),
            list(settled),
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


__all__ = ["OperatorIssueCommandRunner"]
