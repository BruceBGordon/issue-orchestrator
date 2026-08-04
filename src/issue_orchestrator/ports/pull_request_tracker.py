"""Pull request tracker port for PR operations.

This module defines the protocol (interface) for pull request tracking operations
and the PRInfo data class for representing pull request data.

Naming: "Tracker" implies external system CRUD operations, not internal storage.
This is an execution-layer interface.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol


class ReviewState(Enum):
    """GitHub PR review states."""

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"
    PENDING = "PENDING"


# GitHub `statusCheckRollup.state` values (uppercase, as returned by GraphQL).
# Used to disambiguate `mergeable_state == "unstable" | "blocked"` between
# "checks still running" (PENDING/EXPECTED) and "a check actually failed"
# (FAILURE/ERROR). None means we did not fetch / repository has no checks.
StatusCheckRollupState = Literal[
    "SUCCESS", "FAILURE", "PENDING", "EXPECTED", "ERROR"
]


# Capability outcome of a status-check-rollup read. Distinguishes a token
# that *cannot* read check status from a repo that *has* no checks, so the
# reconciler never hides a missing permission behind a "no checks" default.
StatusCheckRollupCapability = Literal["ok", "permission_denied", "transient_error"]


@dataclass(frozen=True)
class StatusCheckRollupRead:
    """Typed outcome of reading a PR head-commit status-check rollup.

    The awaiting-merge reconciler must treat three cases differently, so
    this read never collapses them into a bare ``state | None``:

    - ``ok``: the read succeeded. ``state`` is the rollup state, or
      ``None`` when the PR/commit genuinely has no checks configured.
    - ``permission_denied``: the configured GitHub token lacks the
      capability to read check status (e.g. a fine-grained PAT missing
      the Checks / commit-status read scope). This is a persistent
      operator problem, not a transient blip — callers surface it loudly
      and bound retries instead of silently defaulting to "no checks".
    - ``transient_error``: an ordinary GitHub failure (5xx, timeout).
      Safe to retry next tick; callers treat it as "rollup not yet
      known" (PENDING-equivalent) rather than escalating.

    ``primary_source_denied`` is ``True`` when the primary (GraphQL)
    rollup source was itself permission-denied on this read, *regardless*
    of whether a fallback source (REST check-runs / combined status) then
    produced a usable answer. The capability-owning gate keys its
    repo-wide GraphQL backoff off this flag — so it can stop re-probing a
    scope-blocked GraphQL source even when a fallback source saved this
    particular read, without suppressing that still-classifying fallback.
    A whole-read ``permission_denied`` can only arise when the primary
    source was denied (and no fallback could read a result), so the two
    facts are kept consistent (enforced in ``__post_init__``).
    """

    state: StatusCheckRollupState | None
    capability: StatusCheckRollupCapability = "ok"
    primary_source_denied: bool = False

    def __post_init__(self) -> None:
        # A whole-read permission denial can only happen when the primary
        # (GraphQL) source was denied AND no fallback source could read a
        # result, so the two facts must never disagree.
        if self.capability == "permission_denied" and not self.primary_source_denied:
            raise ValueError(
                "permission_denied rollup read must set primary_source_denied=True"
            )

    @property
    def permission_denied(self) -> bool:
        return self.capability == "permission_denied"


# GitHub `MergeQueueEntry.state` values (uppercase, as returned by GraphQL).
# A PR that is not in the merge queue has no entry at all (``None``).
MergeQueueEntryState = Literal[
    "QUEUED", "AWAITING_CHECKS", "MERGEABLE", "PENDING", "LOCKED", "UNMERGEABLE"
]

# Entry states that mean the PR is still progressing through the queue and the
# orchestrator should observe rather than re-enqueue or rework.
_ACTIVE_MERGE_QUEUE_STATES: tuple[MergeQueueEntryState, ...] = (
    "QUEUED",
    "AWAITING_CHECKS",
    "MERGEABLE",
    "PENDING",
    "LOCKED",
)


@dataclass(frozen=True)
class MergeQueueEntry:
    """A PR's current state/position in the provider's merge queue.

    Carried by a ``PRESENT`` :class:`MergeQueueRead` from
    :meth:`PullRequestTracker.read_merge_queue_entry`.
    """

    state: MergeQueueEntryState
    position: int | None = None

    @property
    def is_active(self) -> bool:
        """True while the entry is still progressing through the queue."""
        return self.state in _ACTIVE_MERGE_QUEUE_STATES

    @property
    def is_failed(self) -> bool:
        """True when GitHub has determined the entry cannot be merged."""
        return self.state == "UNMERGEABLE"


# Outcome kind of reading a PR's merge queue entry. Mirrors the three-valued
# discipline of ``StatusCheckRollupRead``: callers must NOT collapse "could not
# determine the queue state" into "not enqueued", because that turns a transient
# read failure (or an unmodeled provider state) into an actionable "enqueue this
# PR" / "rework this PR" decision based on stale PR status.
MergeQueueReadStatus = Literal["PRESENT", "ABSENT", "INDETERMINATE"]


@dataclass(frozen=True)
class MergeQueueRead:
    """Typed outcome of reading a PR's merge queue entry.

    The merge queue coordinator must treat three cases differently, so this read
    never collapses them into a bare ``MergeQueueEntry | None``:

    - ``PRESENT``: the PR is in the queue; ``entry`` holds its state/position.
    - ``ABSENT``: the provider confirms the PR is **not** in the queue. Only this
      outcome may drive a fresh enqueue/rework/escalation decision.
    - ``INDETERMINATE``: the queue state could not be determined — the read
      failed (transient/auth) or the provider reported a state we do not model.
      Callers must treat this as non-actionable (wait/observe next tick); it is
      never "not enqueued".
    """

    status: MergeQueueReadStatus
    entry: MergeQueueEntry | None = None

    @staticmethod
    def present(entry: MergeQueueEntry) -> "MergeQueueRead":
        return MergeQueueRead("PRESENT", entry)

    @staticmethod
    def absent() -> "MergeQueueRead":
        return MergeQueueRead("ABSENT")

    @staticmethod
    def indeterminate() -> "MergeQueueRead":
        return MergeQueueRead("INDETERMINATE")

    @property
    def is_indeterminate(self) -> bool:
        return self.status == "INDETERMINATE"

    @property
    def is_present(self) -> bool:
        return self.status == "PRESENT"


@dataclass
class PRInfo:
    """Information about a pull request.

    Attributes:
        number: The PR number.
        title: The PR title.
        url: The URL to view the PR.
        branch: The head branch name (source branch).
        body: The PR description/body text.
        state: The PR state - "open", "closed", or "merged".
        labels: List of label names on the PR.
        draft: Whether the PR is in draft state.
        mergeable_state: GitHub's `mergeable_state` (clean/dirty/behind/unstable/
            blocked/...). Says merge readiness.
        status_check_rollup: Aggregated state of required + non-required checks
            on the PR head commit. Says check truth. `None` when not fetched
            (only the single-PR `get_pr` path populates this). Read-capability
            (token cannot read check status vs. no checks configured) is carried
            separately by :class:`StatusCheckRollupRead`, not on this field.
        base_branch: The PR's base (target) branch. Carried so publish/reuse
            decisions can confirm an existing PR targets the branch the stack
            publish gate requires (ADR-0029 / #6596). `None` when the source did
            not provide it.
        merged_at: ISO-8601 merge timestamp for a merged PR. Carried so the
            close-on-merge fallback can ask for close-event evidence at/after
            the merge instead of inferring from issue state alone. `None` when
            unmerged or when the source did not provide it.
    """

    number: int
    title: str
    url: str
    branch: str
    body: str
    state: str  # "open", "closed", "merged"
    labels: list[str]
    draft: bool | None = None
    mergeable_state: str | None = None
    status_check_rollup: StatusCheckRollupState | None = None
    base_branch: str | None = None
    merged_at: str | None = None

    @property
    def is_closed_unmerged(self) -> bool:
        """True when the PR is closed *without* having been merged.

        This is the sole precondition for applying ``blocked:pr-closed``.
        GitHub's raw REST ``state`` field is only ``"open"``/``"closed"`` — a
        merged PR is reported as ``"closed"``. Adapters normalize a merged PR's
        state to ``"merged"`` (from ``merged``/``merged_at``), so a ``"closed"``
        state here genuinely means closed without a merge — a merged or
        still-open PR is never mistaken for one.
        """
        return (self.state or "").strip().lower() == "closed"


@dataclass(frozen=True)
class PRRef:
    """Lightweight pull-request reference sourced directly from a search result.

    Carries only the fields GitHub's ``/search/issues`` response provides
    without a per-PR fetch: ``number``, ``url``, ``title``, ``body``. Use this
    for cheap "which PR is this" lookups — e.g. matching the orchestrator body
    marker — where a full :class:`PRInfo` is not needed. Resolving a list of
    refs costs one search call regardless of how many PRs match, versus the
    one-fetch-per-candidate that :meth:`PullRequestTracker.get_prs_for_issue`
    pays to hydrate full ``PRInfo`` objects.

    The head ``branch``, ``mergeable_state``, and check-rollup of a full
    ``PRInfo`` are intentionally absent here — needing them is the signal to
    use ``get_prs_for_issue``/``get_pr`` instead.
    """

    number: int
    url: str
    title: str
    body: str


class PullRequestTracker(Protocol):
    """Protocol for pull request tracking operations.

    This protocol defines the interface for creating, retrieving, and managing
    pull requests. Implementations can use different platforms (GitHub API,
    GitLab API, etc.) while maintaining the same interface.

    Naming: "Tracker" (not "Repository") because this represents an external
    platform's API, not internal persistence.
    """

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests for a specific branch.

        Args:
            branch: The head branch name to search for.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs with the specified head branch.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get all pull requests associated with a specific issue.

        Finds PRs where:
        - Branch starts with the issue number followed by a dash (e.g., "328-feature-name")
        - OR title contains "#issue_number" (e.g., "#328: Feature")

        Args:
            issue_number: The issue number to find PRs for.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs associated with the issue.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def search_pr_refs_for_issue(self, issue_number: int) -> list["PRRef"]:
        """Return lightweight PR references for an issue using a single search.

        Same association rule as :meth:`get_prs_for_issue` (head branch starts
        with the issue number, or the PR references ``#issue_number``), but does
        NOT hydrate each candidate with a per-PR ``GET`` — it maps the search
        result items directly to :class:`PRRef`. This is one GitHub call
        regardless of how many PRs match, intended for body/marker matching
        where the full ``PRInfo`` (head branch, mergeable state, check rollup)
        is not needed. Returns PRs in any state.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests with a specific label.

        Args:
            label: The label name to filter by.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs with the specified label.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_pr(self, pr_number: int) -> PRInfo | None:
        """Get a specific pull request by number.

        Note: ``status_check_rollup`` is not populated by this method.
        Callers that need check-status visibility (the awaiting-merge
        post-publish classifier) should use
        ``read_pr_status_check_rollup`` instead — that variant pays an
        extra GraphQL round-trip per call and classifies failures.

        Args:
            pr_number: The PR number to retrieve.

        Returns:
            The PRInfo object if found, None otherwise.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def read_pr_status_check_rollup(
        self, pr_number: int, *, skip_primary_source: bool = False
    ) -> "StatusCheckRollupRead":
        """Read a PR's head-commit status-check rollup, classifying failures.

        Costs one GraphQL round-trip on the happy path. Implementations may
        fall back to other status sources after a GraphQL capability failure,
        but callers must invoke this ONLY when the rollup is decisive for a
        merge-readiness decision — the awaiting-merge reconciler fetches it
        solely for reviewer-approved PRs whose ``mergeable_state`` is
        ``unstable`` or ``blocked``. Terminal (closed/merged) and
        ``clean``/``dirty``/``behind`` PRs must NOT call this; they pay no
        rollup cost.

        When ``skip_primary_source`` is True the primary (GraphQL) probe is
        skipped entirely and only the fallback sources are read. The
        capability-owning gate passes this during a primary-source
        permission-backoff window: the GraphQL scope is known-missing, so
        re-probing it would only waste a round-trip and re-log the same
        permission error, yet the REST check-run/commit-status fallback can
        still classify a now-readable failure. The returned read still carries
        ``primary_source_denied=True`` in that case.

        Unlike a plain PR fetch, this never swallows a permission failure
        into a ``None`` rollup. The returned ``StatusCheckRollupRead``
        distinguishes a token that cannot read check status
        (``permission_denied``) from a repo that has no checks
        (``ok`` with ``state=None``) and from a transient GitHub failure
        (``transient_error``).

        When the primary GraphQL rollup query is inaccessible (e.g. the token
        lacks the scope), implementations should fall back to the REST
        check-runs/combined-status API on the PR head SHA before giving up, so
        completed-failed checks are still detected. Only when every source is
        inaccessible should the read report ``permission_denied`` so callers can
        tell "unreadable" apart from "no checks" (``ok`` with ``state=None``).

        Returns:
            A :class:`StatusCheckRollupRead` describing the rollup state
            and the read capability.
        """
        ...

    def enqueue_to_merge_queue(self, pr_number: int) -> None:
        """Add a pull request to the provider's native merge queue.

        Only the merge queue coordinator should call this, and only for PRs
        that have cleared the orchestrator's approval gate. GitHub remains the
        merge authority — this just hands the PR to the queue; GitHub validates
        required checks against the merge group and performs the protected
        merge. Idempotent from the caller's perspective: enqueueing an
        already-queued PR is a no-op (or a benign provider error the caller
        treats as "already queued").

        Raises:
            RepositoryError: If there's an error reaching the provider.
        """
        ...

    def read_merge_queue_entry(self, pr_number: int) -> "MergeQueueRead":
        """Read a PR's current merge queue entry as a typed three-valued result.

        Costs one GraphQL round-trip. The coordinator uses this to decide
        between enqueueing a newly-eligible PR, observing one already in the
        queue, and routing a failed (``UNMERGEABLE``) entry through the
        configured failure policy.

        Returns:
            A :class:`MergeQueueRead`. ``PRESENT`` carries the
            :class:`MergeQueueEntry`; ``ABSENT`` means the provider confirms the
            PR is not enqueued; ``INDETERMINATE`` means the provider reported a
            state we do not model (the queue state could not be determined and
            must not be treated as "not enqueued").

        Raises:
            RepositoryError: If there's an error reaching the provider. Callers
                that must not act on an unknown queue state (the coordinator)
                map this to ``INDETERMINATE`` rather than to ``ABSENT``.
        """
        ...

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List pull requests.

        Args:
            state: Filter by PR state ("open", "closed", "merged", or "all").
                  Defaults to "open".
            limit: Maximum number of PRs to return. Defaults to 100.

        Returns:
            A list of PRInfo objects.
            Returns empty list if no PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None
    ) -> PRInfo:
        """Create a new pull request.

        Args:
            title: The title for the new PR.
            body: The description/body text for the PR.
            head: The head branch name (source branch with changes).
            base: The base branch name (target branch). Defaults to "main".
            draft: Whether to create the PR as a draft. Defaults to None (provider default).

        Returns:
            A PRInfo object representing the newly created PR.

        Raises:
            RepositoryError: If there's an error creating the PR.
            BranchNotFoundError: If the head or base branch doesn't exist.
            ValidationError: If the title or body is invalid.
        """
        ...

    def set_pr_draft(self, pr_number: int, draft: bool) -> None:
        """Set draft status on a pull request.

        Args:
            pr_number: The PR number to update.
            draft: True to mark as draft, False to mark ready for review.
        """
        ...

    def set_pr_base(self, pr_number: int, base: str) -> None:
        """Retarget an existing pull request onto a new base branch.

        Used by the stack publish gate (ADR-0029 / #6596) to bring an existing
        PR's base into line with the branch the gate requires before the PR is
        reused — e.g. when a successor PR was opened against ``main`` but must
        target its predecessor branch, or still targets a predecessor branch
        that has since merged. Idempotent from the caller's perspective:
        retargeting a PR already on ``base`` is a harmless no-op.

        Args:
            pr_number: The PR number to retarget.
            base: The base (target) branch the PR should point at.

        Raises:
            RepositoryError: If there's an error updating the PR.
        """
        ...

    def close_pr(self, pr_number: int) -> None:
        """Close a pull request.

        Args:
            pr_number: The PR number to close.

        Raises:
            RepositoryError: If there's an error closing the PR.
        """
        ...

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment to an issue or pull request.

        Args:
            issue_or_pr_number: The issue or PR number to comment on.
            body: The comment text to add.

        Returns:
            The URL of the created comment.

        Raises:
            RepositoryError: If there's an error adding the comment.
            IssueNotFoundError: If the issue/PR doesn't exist.
            ValidationError: If the comment body is empty or invalid.
        """
        ...

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Get all reviews on a pull request.

        Args:
            pr_number: The PR number to get reviews for.

        Returns:
            List of review dicts with 'state', 'body', 'user' etc.
            Returns empty list if no reviews found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...


# Backwards compatibility alias
PRRepository = PullRequestTracker
