"""Owner for recording cleanup facts at the completion handoff."""

from collections.abc import MutableSequence

from ..domain.models import (
    ImmediateCleanup,
    OrchestratorState,
    PendingCleanup,
    Session,
    SessionStatus,
)
from .completion_handler import CleanupDecision, CleanupDisposition


class CompletionCleanupStateOwner:
    """Own cleanup-queue mutation for one completion handoff."""

    def __init__(self, state: OrchestratorState) -> None:
        self._pending: MutableSequence[PendingCleanup] = state.pending_cleanups
        self._immediate: MutableSequence[ImmediateCleanup] = state.immediate_cleanups

    def record(
        self,
        decision: CleanupDecision,
        session: Session,
        effective_status: SessionStatus,
    ) -> None:
        """Record exactly the fact selected by the completion decision owner."""
        if effective_status is not SessionStatus.COMPLETED:
            self._record_immediate(session, effective_status)
            return

        if decision.disposition is CleanupDisposition.DEFERRED:
            assert decision.pending_cleanup is not None
            self._pending.append(decision.pending_cleanup)
            return

        if decision.disposition is CleanupDisposition.IMMEDIATE:
            self._record_immediate(session, effective_status)
            return

        if decision.disposition is CleanupDisposition.NONE:
            return

        raise ValueError(f"Unhandled cleanup disposition: {decision.disposition!r}")

    def _record_immediate(
        self,
        session: Session,
        effective_status: SessionStatus,
    ) -> None:
        self._immediate.append(
            ImmediateCleanup(
                issue_number=session.issue.number,
                terminal_id=session.terminal_id,
                worktree_path=str(session.worktree_path),
                reason=effective_status.value,
                # Disposable tech-lead investigation worktrees are removed
                # on completion regardless of ordinary cleanup settings.
                scratch_worktree=session.scratch_worktree,
            )
        )
