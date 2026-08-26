"""Deciding whether this publish may push a worktree that is still dirty.

The pre-push hook runs in two places the orchestrator controls -- the
pre-publish gate and the push itself -- and a git hook can only see its own
environment and the filesystem. Telling it through the filesystem was wrong:
the worktree is writable by the agent, so a file there is an assertion anyone
can make rather than a decision only the orchestrator can take, and it survives
a hard process exit, which turns a crash into a standing exemption.

So the decision travels as environment on the processes the orchestrator
spawns. This module is where that decision is made; ``domain.dirty_remediation``
owns the rule and the wire format, and the execution adapters carry it.
"""

import logging
from pathlib import Path

from ..domain.dirty_remediation import (
    DIRTY_ESCALATION_ENV,
    DirtyTreeDisposition,
    dirty_escalation_signal,
    dirty_tree_disposition,
)
from ..domain.models import CompletionRecord
from .completion_ports import GitAdapter

logger = logging.getLogger(__name__)


def dirty_escalation_env(
    git_adapter: GitAdapter, worktree: Path, record: CompletionRecord
) -> dict[str, str]:
    """The signal authorizing a dirty push of ``worktree``, for escalations only.

    Empty for every other completion, so the guard stays armed. The value names
    this worktree and its current HEAD, so it authorizes exactly the push about
    to be made: a rebase retry that moves HEAD re-derives it, and a value seen
    in one context cannot be replayed against another commit. An unresolvable
    HEAD yields no signal at all rather than an unbound one.
    """
    if dirty_tree_disposition(record.outcome.value) is not (
        DirtyTreeDisposition.PRESERVE_AND_ESCALATE
    ):
        return {}

    head_sha = git_adapter.get_head_sha(worktree)
    if not head_sha:
        logger.warning(
            "Could not resolve HEAD for %s; not authorizing a dirty push", worktree
        )
        return {}

    logger.info(
        "Authorizing dirty push for %s at %s (outcome=%s)",
        worktree,
        head_sha,
        record.outcome.value,
    )
    return {
        DIRTY_ESCALATION_ENV: dirty_escalation_signal(str(worktree.resolve()), head_sha)
    }
