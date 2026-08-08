"""Composition of the persistent review-exchange pair registry.

One wiring shared by the production and testing composition roots, kept out of
:mod:`.bootstrap` because it depends on nothing that root owns: it builds the
registry and the worktree-reclaim hook that fires on the same lifecycle
boundary which closes the pair's subprocesses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..execution.persistent_exchange_pair_registry_inmemory import (
        InMemoryPersistentExchangePairRegistry,
    )


def build_pair_registry_with_worktree_hook() -> "InMemoryPersistentExchangePairRegistry":
    """Build the persistent pair registry with the worktree-reclaim hook.

    The hook reclaims the reviewer worktree at the same lifecycle
    boundary that closes the subprocesses (PR #6212 review feedback).
    Without it, B2's removal of the per-exchange
    ``remove_reviewer_worktree`` call would leave sibling worktrees
    on disk after every release path (escalation / reset / shutdown
    / merge). Hook is best-effort: errors are logged inside the
    registry's ``_tear_down`` so a failed ``git worktree remove``
    doesn't mask whatever brought us into the release path.

    Extracted to a module-level helper so the production and testing
    bootstrap paths share one wiring (single owner) and so tests
    that patch ``remove_reviewer_worktree`` at the source module see
    a real registry that delegates to the patched function.
    """
    from ..execution.persistent_exchange_pair_registry_inmemory import (
        InMemoryPersistentExchangePairRegistry,
        PersistentExchangePair,
    )

    def _reclaim_reviewer_worktree(
        pair: PersistentExchangePair, reason: str,
    ) -> None:
        # Resolve the worktree helpers lazily inside the hook so
        # tests can patch
        # ``issue_orchestrator.execution.reviewer_worktree.remove_reviewer_worktree``
        # at the source module and have the patch take effect.
        # ``coder_branch`` is unused by ``remove_reviewer_worktree`` —
        # it only needs the path — so stamping a placeholder keeps
        # the helper's existing signature working without forcing the
        # pair to remember the branch.
        from ..execution import reviewer_worktree

        del reason  # only used in the registry's structured log
        reviewer_worktree.remove_reviewer_worktree(
            reviewer_worktree.ReviewerWorktree(
                path=pair.reviewer_worktree_path,
                coder_branch="<unused-on-removal>",
            ),
            force=True,
        )

    return InMemoryPersistentExchangePairRegistry(
        on_release=_reclaim_reviewer_worktree,
    )



__all__ = ["build_pair_registry_with_worktree_hook"]
