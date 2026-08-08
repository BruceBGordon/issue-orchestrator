"""Compose the operator retry/dismiss commands (#6999 F5/A2).

Assembled here, at the composition root, because the command's collaborators are
exactly the container's: the label owner, the label registry, the fresh issue
reader, the queue-cache store. Only the live state and the lock guarding it come
from the facade, and those arrive through the factory call.

The point of routing both endpoints through one composed command is that the
transport can no longer reach past it: there is nowhere for the HTTP layer to
learn the retry-history representation or the queue cache from, so the
"labels first, local state only if that committed" ordering has one home.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..control.needs_human_block import SharedNeedsHumanBlock
    from ..control.label_manager import LabelManager
    from ..infra.config import Config
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.operator_issue_commands import OperatorIssueCommandFactory
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.repository_host import RepositoryHost


def build_operator_issue_command_factory(
    config: "Config",
    *,
    repository_host: "RepositoryHost",
    label_manager: "LabelManager",
    needs_human_block: "SharedNeedsHumanBlock",
    fresh_issue_reader: "FreshIssueReader",
    queue_cache_store: "QueueCacheStore",
) -> "OperatorIssueCommandFactory":
    """Implement ``ports.operator_issue_commands.OperatorIssueCommandFactory``."""
    from ..control.operator_issue_command_runner import OperatorIssueCommandRunner
    from ..control.operator_unblock import OperatorUnblocker

    unblocker = OperatorUnblocker(
        repository_host=repository_host,
        labels=label_manager,
        block=needs_human_block,
    )

    def factory(*, state, run_locked):
        return OperatorIssueCommandRunner(
            unblocker=unblocker,
            fresh_labels=fresh_issue_reader,
            config=config,
            queue_cache_store=queue_cache_store,
            state=state,
            run_locked=run_locked,
        )

    return factory


__all__ = ["build_operator_issue_command_factory"]
