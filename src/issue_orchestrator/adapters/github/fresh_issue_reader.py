"""GitHub adapter for fresh issue label reads."""

import logging

from ...infra import gh_audit
from ...infra.config import Config
from ...ports.fresh_issue_reader import FreshIssueReadError, FreshIssueReader
from .errors import GitHubHttpError
from .http_client import GitHubHttpClient, GitHubHttpConfig, build_github_auth
from .repo import get_repo_from_git, GitRepoError

logger = logging.getLogger(__name__)


class GitHubFreshIssueReader(FreshIssueReader):
    """FreshIssueReader implementation for GitHub."""

    def __init__(
        self,
        repo: str | None = None,
        config: Config | None = None,
        *,
        http_client: GitHubHttpClient | None = None,
    ) -> None:
        """``http_client`` is the injection seam for the read-failure contract.

        This adapter's whole correctness contribution is what it does when the
        read FAILS (#6957 round-2 review F4/A4), and that is only testable with
        a client that fails. Production leaves it None and the client is built
        from config exactly as before.
        """
        if repo:
            self.repo = repo
        else:
            try:
                self.repo = get_repo_from_git()
            except GitRepoError as exc:
                raise GitHubHttpError(f"Failed to resolve repo: {exc}") from exc

        if http_client is not None:
            self._client = http_client
            return

        auth_kwargs = config.github_auth_kwargs() if config else {}
        auth = build_github_auth(
            **auth_kwargs,
            repo=self.repo,
            api_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
            timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
        )
        self._client = GitHubHttpClient(
            GitHubHttpConfig(
                repo=self.repo,
                base_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
                timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
                auth=auth,
            )
        )

    def read_issue_labels(self, issue_number: int) -> list[str]:
        """The issue's labels right now, or raise — never a silent ``[]``.

        This used to return ``[]`` for a timeout, a rate limit, or an auth
        failure, which the reconciliation gate then read as "observed: no
        labels". An empty set satisfies the tech-lead mutation expectation
        (which forbids ``io:needs-reconcile`` and requires nothing), so a failed
        read let the control plane mutate straight through an explicit operator
        pause (#6957 round-2 review F4/A4). Failure is now propagated as
        :class:`FreshIssueReadError` so each caller decides what unknown means.
        """
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_READ,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                return self._client.get_issue_labels(issue_number, use_cache=False)
        except GitHubHttpError as exc:
            logger.error("Failed to read fresh labels for issue %s: %s", issue_number, exc)
            raise FreshIssueReadError(
                f"could not read fresh labels for issue #{issue_number}: {exc}"
            ) from exc
        except Exception as exc:
            logger.error("Unexpected error reading fresh labels for issue %s: %s", issue_number, exc)
            raise FreshIssueReadError(
                f"could not read fresh labels for issue #{issue_number}: {exc}"
            ) from exc
