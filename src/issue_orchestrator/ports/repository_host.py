"""Repository host port - combined interface for repository operations.

This module defines a combined protocol for all repository operations
that the orchestrator needs. This allows the orchestrator to depend on
a single port interface rather than importing concrete adapters.

Naming: "RepositoryHost" represents the external platform that hosts
issues, PRs, and labels. It combines IssueTracker, LabelSet, and
PullRequestTracker into a single interface.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .issue_tracker import IssueTracker
from .label_set import LabelSet
from .pull_request_tracker import PullRequestTracker

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey


RepositoryHostErrorKind = Literal["http", "transport", "other"]


@dataclass(frozen=True)
class DependencyIssueSnapshot:
    """Issue facts needed to evaluate dependency gating."""

    state: str
    milestone: str | None


class RepositoryHostError(Exception):
    """Base exception for repository host access failures."""

    host: str = "repository"
    kind: RepositoryHostErrorKind = "other"


class RepositoryScanIncompleteError(RepositoryHostError):
    """An exhaustive scan could not prove it saw everything.

    Categorically different from "could not reach the host": the request
    SUCCEEDED enough to reveal that the result would be a truncated or partial
    view — a page cap exhausted, a mid-scan non-200, a malformed page. Callers
    that degrade gracefully on an outage must still fail loud on this, because
    acting on a silently-incomplete list is worse than not acting at all.

    Declared at the port so control-layer policy can distinguish the two without
    importing an adapter.
    """


def repository_host_failure_status(exc: RepositoryHostError) -> int:
    """Map repository-host failures to client-facing HTTP status codes."""
    status_code = getattr(exc, "status_code", None)
    if status_code in {401, 403, 429}:
        return status_code
    if isinstance(status_code, int):
        return 502
    return 503


def repository_host_failure_payload(
    exc: RepositoryHostError,
    *,
    message: str | None = None,
) -> dict[str, Any]:
    """Build a stable JSON error payload for repository-host failures."""
    status_code = getattr(exc, "status_code", None)
    response_text = getattr(exc, "response_text", None)
    method = getattr(exc, "method", None)
    url = getattr(exc, "url", None)
    payload: dict[str, Any] = {
        "error": message or _repository_host_default_error(exc),
        "error_code": _repository_host_error_code(exc),
        "detail": _bounded_detail(str(response_text or exc)),
    }
    if isinstance(status_code, int):
        payload["upstream_status_code"] = status_code
    if method:
        payload["method"] = method
    if url:
        payload["url"] = url
    return payload


def _repository_host_default_error(exc: RepositoryHostError) -> str:
    if getattr(exc, "host", None) == "github":
        return "GitHub issue query failed"
    return "Repository issue query failed"


def _repository_host_error_code(exc: RepositoryHostError) -> str:
    host = getattr(exc, "host", "repository")
    kind = getattr(exc, "kind", "other")
    if host == "github":
        if kind == "transport":
            return "github_transport_error"
        if kind == "http":
            return "github_http_error"
    if kind == "transport":
        return "repository_transport_error"
    if kind == "http":
        return "repository_http_error"
    return "repository_host_error"


def _bounded_detail(value: str) -> str:
    max_len = 1000
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]}..."


class RepositoryHost(IssueTracker, LabelSet, PullRequestTracker, Protocol):
    """Combined protocol for all repository operations.

    This protocol extends IssueTracker, LabelSet, and PullRequestTracker
    to provide a unified interface for the orchestrator.

    Use this when you need access to issues, labels, and PRs from a single
    dependency. The orchestrator accepts this instead of concrete adapters.

    GitHubAdapter implements this protocol by implementing all three
    component protocols plus dependency-checking issue fact methods.
    """

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        """Get issue facts needed by dependency evaluation.

        This method is used by DependencyEvaluator to check both dependency
        completion state and milestone scope with one repository-host read.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo
                  dependencies. If None, uses the default repo.

        Returns:
            DependencyIssueSnapshot, or None if the issue doesn't exist.
        """
        ...

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue ('open', 'closed', or None if not found).

        This method is used by DependencyEvaluator to check if blocking
        issues are closed.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo
                  dependencies. If None, uses the default repo.

        Returns:
            'open', 'closed', or None if the issue doesn't exist.
        """
        ...

    def get_default_branch(self) -> str:
        """The repository's real default branch name (e.g. 'main', 'master', 'trunk').

        Authoritative source for the branch an agent should verify merge-
        reachability against (``origin/<default_branch>``). Implementations should
        cache it — the default branch is effectively constant for a session.
        """
        ...

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the milestone name of an issue (or None if no milestone).

        This method is used by DependencyEvaluator to validate milestone scope.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo
                  dependencies. If None, uses the default repo.

        Returns:
            The milestone name (title), or None if no milestone assigned.
        """
        ...

    def create_issue_key(self, issue_number: int) -> "IssueKey":
        """Create an IssueKey for the given issue number.

        The adapter creates the appropriate IssueKey implementation
        for its backing store (e.g., GitHubIssueKey for GitHub).

        Args:
            issue_number: The issue number to create a key for.

        Returns:
            An IssueKey implementation appropriate for this repository host.
        """
        ...

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict[str, Any] | None:
        """Create a new issue.

        Args:
            title: Issue title
            body: Issue body
            labels: Labels to add
            milestone: Milestone number to assign

        Returns:
            Issue data dict with 'number', 'html_url', etc. or None on failure
        """
        ...

    def find_issue_by_marker(
        self, *, title: str, marker: str, authoritative: bool = False
    ) -> int | None:
        """Recover an orchestrator-created issue by its body marker (#6957).

        Every "create the remote issue, then record the local ledger row"
        boundary can die in between, leaving a real issue no ledger knows
        about. This is how the retry finds that issue instead of filing a
        second one, so a create-once invariant survives the crash window.

        Ownership is decided by *marker* appearing in the issue BODY — never by
        the mutable title or labels.

        ``authoritative`` selects which of two contracts a NEGATIVE answer
        carries, mirroring ``list_issues(exhaustive=...)``:

        * ``False`` (default) — best-effort. A hit is real; a MISS IS NOT PROOF.
          The scan is bounded and its fallback is title-scoped, so an aged-out,
          retitled issue is invisible. Use only where a miss costs at most a
          duplicate some other invariant already prevents.
        * ``True`` — a miss means PROVEN ABSENT. Title-independent and complete
          or loud: incompleteness raises rather than answering "no". Required
          wherever a negative answer is load-bearing, such as retiring a durable
          creation intent (#6957 round-5 review F13).

        Returns:
            The existing issue's number, or None per the contract selected above.

        Raises:
            RepositoryHostError: If the lookup cannot be performed, or (when
                ``authoritative``) if completeness cannot be established.
                Callers must treat that as "unknown", never as "no issue exists"
                — creating a duplicate is the failure this exists to prevent.
        """
        ...

    def create_milestone(
        self,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
        state: str = "open",
    ) -> dict[str, Any] | None:
        """Create a milestone.

        Args:
            title: Milestone title
            description: Optional description
            due_on: Optional ISO timestamp
            state: Milestone state ("open" or "closed")

        Returns:
            Milestone data dict with 'number', 'title', etc. or None on failure
        """
        ...

    def update_label_cache(self, issue_number: int, labels: list[str]) -> None:
        """Update cached labels for an issue.

        Adapters may implement a local cache to avoid repeated GH reads.
        """
        ...

    def list_milestones(self, state: str = "open") -> list[dict[str, Any]]:
        """List milestones in the repository.

        Args:
            state: Filter by milestone state ('open', 'closed', 'all')

        Returns:
            List of milestone dictionaries with 'number', 'title', 'description', etc.
        """
        ...

    def update_issue_milestone(self, issue_number: int, milestone: int | None) -> None:
        """Assign or clear a milestone on an issue.

        Args:
            issue_number: The issue number to update
            milestone: Milestone number to assign, or None to clear
        """
        ...

    def list_labels(self) -> list[dict[str, Any]]:
        """List all labels in the repository.

        Returns:
            List of label dictionaries with 'name', 'color', 'description' keys.
        """
        ...

    def create_label(
        self,
        name: str,
        *,
        color: str = "ededed",
        description: str | None = None,
        force: bool = False,
    ) -> None:
        """Create a label in the repository.

        Args:
            name: Label name
            color: Hex color code (without #)
            description: Optional label description
            force: If True, update existing label; if False, skip if exists
        """
        ...

    def update_issue_state(self, issue_number: int, state: str) -> None:
        """Update an issue's state (open/closed).

        Args:
            issue_number: The issue number to update.
            state: New state ("open" or "closed").
        """
        ...

    def get_rate_limit_snapshot(self) -> dict[str, Any] | None:
        """Get current GitHub API rate limit snapshot.

        Returns:
            Dictionary with rate limit info, or None if unavailable.
        """
        ...
