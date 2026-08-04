"""GitHub implementation of the finding-promotion target port (#6957).

The orchestrator's ``GitHubAdapter`` is bound to ONE repo (its caches, label
provisioning, and write verification all assume ``config.repo``), but promotion
routes a finding to the repo that owns the fix — frequently a different one. So
this adapter reuses the adapter's own cross-repo pattern (a short-lived
``GitHubHttpClient`` built from the same auth/base-url config), scoped to the
four operations :class:`PromotionTargetHost` declares and nothing else.

Requests for the adapter's OWN repo are delegated to the bound adapter, so a
self-routed promotion keeps the cache/verification behavior every other write
in that repo gets.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Sequence

from ...ports.promotion_target import FiledIssue, PromotedIssueOutcome
from .errors import GitHubHttpError
from .http_client import GitHubHttpClient, GitHubHttpConfig

if TYPE_CHECKING:
    from .github_adapter import GitHubAdapter

logger = logging.getLogger(__name__)

# Fallback color/description for labels this adapter provisions in a target
# repo. Concrete tech-lead label metadata is owned by the label manager for the
# managed repo; a foreign repo gets a neutral, self-describing entry.
_PROMOTION_LABEL_COLOR = "ededed"
_PROMOTION_LABEL_DESCRIPTION = "Applied by issue-orchestrator finding promotion"


class GitHubPromotionTargetHost:
    """Files and follows promoted findings in an arbitrary GitHub repository."""

    def __init__(self, adapter: "GitHubAdapter") -> None:
        self._adapter = adapter

    @contextmanager
    def _client_for(self, repo: str) -> Iterator[GitHubHttpClient]:
        """A client scoped to *repo*, reusing the adapter's own for its repo."""
        if repo == self._adapter.repo:
            yield self._adapter.http_client
            return
        config = self._adapter.http_client.config
        client = GitHubHttpClient(
            GitHubHttpConfig(
                repo=repo,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
                auth=config.auth,
            )
        )
        try:
            yield client
        finally:
            client.close()

    def check_writable(self, *, repo: str) -> str | None:
        """None when the token can create issues in *repo*, else the reason.

        GitHub reports effective permissions on the repository payload, so this
        is one cheap read per configured route target at startup — far better
        than discovering the problem on the tick a pattern finally crosses its
        evidence threshold. A repo whose payload omits ``permissions`` (some
        token kinds) is treated as writable: the applier's filing failure is
        still loud, and refusing to start on an inconclusive read would be
        worse than a late error.
        """
        try:
            with self._client_for(repo) as client:
                payload = client.get_repository()
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return (
                    f"{repo} was not found (or the token cannot see it);"
                    " check the route target and the token's repo access"
                )
            return f"{repo} could not be read: {exc}"
        except Exception as exc:  # transport/auth failures
            return f"{repo} could not be read: {exc}"
        if not isinstance(payload, dict):
            return f"{repo} returned an unexpected repository payload"
        if payload.get("has_issues") is False:
            return f"{repo} has issues disabled, so findings cannot be filed there"
        permissions = payload.get("permissions")
        if isinstance(permissions, dict) and not (
            permissions.get("push") or permissions.get("admin")
        ):
            return (
                f"{repo} is readable but not writable by this token; finding"
                " promotion needs issue-write access"
            )
        return None

    def file_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> FiledIssue:
        """Create the promotion issue, provisioning its labels first.

        Provisioning precedes creation for the same reason the tech-lead issue
        creation boundary does it: GitHub silently DROPS unknown labels, and a
        promotion whose gate label was dropped would be an immediately
        schedulable issue nobody approved.
        """
        with self._client_for(repo) as client:
            self._ensure_labels(client, repo=repo, labels=labels)
            result = client.create_issue(title=title, body=body, labels=list(labels))
        if not isinstance(result, dict) or not result.get("number"):
            raise GitHubHttpError(
                f"filing a promoted finding in {repo} returned no issue number"
            )
        return FiledIssue(
            number=int(result["number"]),
            url=str(result.get("html_url") or ""),
        )

    @staticmethod
    def _ensure_labels(
        client: GitHubHttpClient, *, repo: str, labels: Sequence[str]
    ) -> None:
        existing = {
            name.casefold()
            for entry in client.list_all_labels()
            if isinstance(entry, dict)
            and isinstance((name := entry.get("name")), str)
        }
        for label in labels:
            if label.casefold() in existing:
                continue
            client.create_label(
                label,
                color=_PROMOTION_LABEL_COLOR,
                description=_PROMOTION_LABEL_DESCRIPTION,
            )
            existing.add(label.casefold())
            logger.info(
                "[tech_lead] Provisioned label %r in promotion target %s",
                label,
                repo,
            )

    def add_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        with self._client_for(repo) as client:
            client.add_comment(issue_number, body)

    def read_outcome(
        self, *, repo: str, issue_number: int
    ) -> PromotedIssueOutcome | None:
        """State of a promoted issue, plus the merged PR that closed it (if any).

        Costs ONE read while the issue is open — the common case — and a second
        (the linked-PR search) only once it has closed, so following a
        promotion is cheap for its whole in-flight lifetime.
        """
        with self._client_for(repo) as client:
            payload = client.get_issue(issue_number)
            if not isinstance(payload, dict):
                return None
            state = payload.get("state")
            if not isinstance(state, str):
                return None
            if state != "closed":
                return PromotedIssueOutcome(state=state)
            return PromotedIssueOutcome(
                state=state,
                merged_pr_url=self._merged_pr_url(client, issue_number),
            )

    @staticmethod
    def _merged_pr_url(client: GitHubHttpClient, issue_number: int) -> str:
        """URL of a MERGED pull request referencing the issue, else "".

        Merged-ness is the whole distinction the loop closure turns on: closed
        with a merged PR is a shipped fix; closed without one is the operator
        declining. A search failure yields "" — which classifies as declined —
        so it is deliberately not swallowed here; the caller's read wrapper
        treats a raised error as "unknown this tick" and leaves the promotion
        in flight instead.
        """
        for item in client.get_prs_for_issue(issue_number):
            if not isinstance(item, dict):
                continue
            pull_request = item.get("pull_request")
            merged_at = (
                pull_request.get("merged_at")
                if isinstance(pull_request, dict)
                else None
            )
            if merged_at:
                url = item.get("html_url")
                return str(url) if isinstance(url, str) else ""
        return ""


def build_promotion_target_host(repository_host: Any) -> Any:
    """Adapt a repository host to :class:`PromotionTargetHost`, or None.

    The composition root wires promotion only when the repository host is a
    real GitHub adapter; anything else (a fake, an offline stub) leaves the lane
    unwired, which makes its actions fail loudly rather than silently no-op.
    """
    from .github_adapter import GitHubAdapter

    if isinstance(repository_host, GitHubAdapter):
        return GitHubPromotionTargetHost(repository_host)
    return None
