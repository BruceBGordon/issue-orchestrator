"""Close-on-merge fallback and awaiting-merge fact builders.

GitHub's closing-keyword parse was the ONLY close-on-merge mechanism. It is
word-boundary sensitive and easily defeated — a hand-authored recovery PR whose
body contained literal ``\\n`` escapes ("...\\n\\nCloses #45.") left the issue
open after its PR merged. The awaiting-merge reconciler shed the stale labels
and terminalized history but issued no close, so the first planning pass after
a restart relaunched a coding session on the already-merged issue (porchpin
case file #81, ``merged-unclosed-issue-relaunched-after-restart``).

The merged-terminal discovery therefore reads the issue's live state once, at
the terminal transition (not per-tick — the #6600 rollup-noise removal is
untouched), and carries ``issue_open`` on the reconciliation fact so the
Planner can order a close on the terminal-recovery owner command. Only a
MERGED PR earns the fallback: closed-unmerged PRs keep their drift-path
behavior, and intentionally reopened issues (porchpin case file #59) are never
touched — their history entries are already terminal and cannot re-fire.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredAwaitingMergeReconciliation,
)
from ..ports.repository_host import RepositoryHostError
from .awaiting_merge_post_publish_policy import normalized_state
from .queue_cache import record_issue_refreshes

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, SessionHistoryEntry
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)


def should_close_merged_issue(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    state: "OrchestratorState",
    entry: "SessionHistoryEntry",
    merged_at: str | None,
    now: float,
) -> bool | None:
    """Whether the issue behind a just-merged PR needs the fallback close.

    True only on positive evidence of the failure this fallback exists for:
    the issue is open AND no ``closed`` event exists at/after the PR's
    ``merged_at`` — i.e. GitHub's auto-close never fired for this merge. An
    open issue that HAS a close event since the merge was auto-closed and then
    deliberately reopened; it is never re-closed. A missing ``merged_at``
    means no evidence either way — never infer a destructive close from
    ``state == open`` alone.

    Returns None when the evidence cannot be read (transient repository-host
    error), so the caller can leave the history entry reconcilable instead of
    finalizing it against a guessed state: fail-open recreates the relaunch
    bug, and raising aborts the whole gather tick. An issue the host reports
    as missing is treated as not-open — there is nothing to close, and
    holding the entry hostage would strand it.
    """
    try:
        issue = get_issue(entry.issue_number)
    except RepositoryHostError:
        logger.warning(
            "Unable to check issue state for merged PR close fallback: "
            "issue=#%d; leaving entry reconcilable for retry",
            entry.issue_number,
        )
        return None
    if issue is None:
        return False
    record_issue_refreshes(state, {entry.issue_number}, now)
    if normalized_state(issue.state) == "closed":
        return False
    if not merged_at:
        logger.warning(
            "Merged PR for issue #%d carries no merged_at; skipping close-on-"
            "merge fallback — open state alone is not evidence of a failed "
            "auto-close",
            entry.issue_number,
        )
        return False
    try:
        auto_close_fired = closed_on_or_after(entry.issue_number, merged_at)
    except RepositoryHostError:
        logger.warning(
            "Unable to read close events for merged PR close fallback: "
            "issue=#%d; leaving entry reconcilable for retry",
            entry.issue_number,
        )
        return None
    if auto_close_fired:
        logger.info(
            "Issue #%d was closed at/after its PR merge and deliberately "
            "reopened; close-on-merge fallback will not re-close it",
            entry.issue_number,
        )
        return False
    return True


def close_on_merge_comment(pr_url: str, pr_number: int) -> str:
    """Explanatory comment posted when the fallback closes an issue."""
    return (
        f"Closing: {pr_url or f'PR #{pr_number}'} merged this issue's work, "
        "but the PR registered no closing reference, so GitHub did not "
        "auto-close the issue. The orchestrator closed it during "
        "awaiting-merge reconciliation. If this issue was intentionally left "
        "open for remaining scope, reopen it."
    )


def reconciliation_fact(
    *,
    entry: "SessionHistoryEntry",
    pr_number: int,
    status: AwaitingMergeTerminalStatus,
    reason: str,
    source: AwaitingMergeReconciliationSource,
    issue_open: bool = False,
) -> DiscoveredAwaitingMergeReconciliation:
    return DiscoveredAwaitingMergeReconciliation(
        issue_number=entry.issue_number,
        pr_number=pr_number,
        pr_url=entry.pr_url or "",
        status=status,
        status_reason=reason,
        source=source,
        issue_open=issue_open,
    )


def pr_terminal_reason(status: AwaitingMergeTerminalStatus) -> str:
    if status == "merged":
        return "PR merged; awaiting merge reconciled"
    return "PR closed; awaiting merge reconciled"
