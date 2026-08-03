"""Issue tracker port for accessing issue data.

This module defines the protocol (interface) for issue tracking operations.
Implementations of this protocol can use different platforms (GitHub, GitLab,
Jira, etc.) while maintaining the same interface.

Naming: "Tracker" implies external system CRUD operations, not internal storage.
This is an execution-layer interface.
"""

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .issue import Issue


class IssueTracker(Protocol):
    """Protocol for issue tracking operations.

    This protocol defines the interface for accessing issues from an external
    tracking system. It provides methods for retrieving and querying issues.

    Naming: "Tracker" (not "Repository") because this represents an external
    system's API, not internal persistence.
    """

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
        *,
        exhaustive: bool = False,
    ) -> list["Issue"]:
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
                   If None, no label filtering is applied.
            milestone: Filter by milestone title. If None, no milestone
                      filtering is applied.
            state: Filter by issue state. Can be "open", "closed", or "all".
                  Defaults to "open".
            limit: Maximum number of issues to return. Defaults to 100.
            required_stable_ids: Optional set of stable IDs that must be discovered.
                If provided and missing after cached fetch, retry without cache.
            exhaustive: If True, the paginated walk must FAIL LOUD rather than
                return a silently partial set (#6779 R17). Authoritative callers
                (the exhaustive open tech-lead-agent anchor scan) pass this so
                planning/recovery can never proceed from a truncated list; a
                later-page HTTP/transport failure or a cap-exhausted scan raises.

        Returns:
            A list of Issue objects matching the criteria.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def list_issues_delta(
        self,
        *,
        since: str,
        limit: int = 100,
    ) -> tuple[list["Issue"], str | None]:
        """List issues updated since the watermark, returning next watermark hint.

        Args:
            since: ISO-8601 watermark to query updates since.
            limit: Maximum number of updated issues to process in this cycle.

        Returns:
            A tuple of (issues, next_watermark). next_watermark is an ISO timestamp
            hint that callers can persist after successful processing.
        """
        ...

    def get_issue(self, issue_number: int) -> "Issue | None":
        """Get a specific issue by number.

        Args:
            issue_number: The issue number to retrieve.

        Returns:
            The Issue object if found, None otherwise.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get the labels for a specific issue.

        Args:
            issue_number: The issue number to get labels for.

        Returns:
            A list of label names for the issue. Returns empty list if
            issue not found or has no labels.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_issue_labels_fresh(self, issue_number: int) -> list[str]:
        """Get the labels for a specific issue, bypassing caches.

        This method is intended for correctness-critical reads where stale
        labels could cause incorrect state transitions.

        Args:
            issue_number: The issue number to get labels for.

        Returns:
            A list of label names for the issue. Returns empty list if
            issue not found or has no labels.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Get the conversation comments on an issue or pull request.

        GitHub serves issue and PR conversation comments from the same
        endpoint, so this also returns the timeline comments on a PR. Used
        by lifecycle code that needs to dedupe orchestrator-authored marker
        comments before re-posting them.

        Args:
            issue_number: The issue or PR number to read comments for.

        Returns:
            A list of comment dictionaries (each with at least a ``body``
            key). Returns an empty list if there are no comments.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def issue_comment_marker_present(self, issue_number: int, marker: str) -> bool:
        """Report whether ``marker`` appears in any comment on the issue/PR.

        Scans **all** pages of the issue/PR conversation comments (GitHub
        serves PR timeline comments from the same endpoint), not just the
        first page, so callers can reliably dedupe an orchestrator-authored
        marker comment that may sit beyond the first 100 comments. The marker
        is matched as a substring of each comment body.

        Args:
            issue_number: The issue or PR number to scan for ``marker``.
            marker: The substring to look for in comment bodies.

        Returns:
            ``True`` if any comment body contains ``marker``; ``False``
            otherwise.

        Raises:
            RepositoryError: If there's an error accessing the data source.
                Callers that dedupe on this fact must let the error propagate
                (fail loud) rather than risk a duplicate or dropped comment.
        """
        ...

    def issue_closed_on_or_after(self, issue_number: int, timestamp: str) -> bool:
        """Report whether the issue has a ``closed`` event at/after ``timestamp``.

        Typed transition evidence for the close-on-merge fallback: a ``closed``
        event at/after a PR's merge proves GitHub's closing-keyword auto-close
        (or an equivalent close) already fired for that merge — a currently
        open issue was therefore deliberately reopened and must not be
        re-closed. ``False`` means the issue was never closed since
        ``timestamp``.

        Args:
            issue_number: The issue number to read close events for.
            timestamp: ISO-8601 UTC timestamp (as provided by the tracker's
                own payloads, e.g. a PR's ``merged_at``).

        Returns:
            ``True`` if a ``closed`` event exists at/after ``timestamp``.

        Raises:
            RepositoryError: If there's an error accessing the data source.
                Callers deciding a destructive write on this fact must treat
                the error as "evidence unavailable", not as either answer.
        """
        ...

    def search_issues_by_title(
        self,
        query_terms: list[str],
        *,
        limit: int = 30,
    ) -> list["Issue"]:
        """Search issues by title substrings, OR'd, scoped to title.

        Used by the resolver as a fallback when scan-based lookup misses an
        external_id. Substring semantics — callers filter for exact matches.

        Args:
            query_terms: Substrings to match against issue titles (OR'd).
            limit: Maximum number of matches to return.

        Returns:
            Matching Issue objects (pull requests excluded).
        """
        ...

# Backwards compatibility alias
IssueRepository = IssueTracker
