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

from ...ports.promotion_target import (
    FiledIssue,
    PromotedIssueOutcome,
    PromotionFilingContract,
    validate_promotion_issue_marker,
)
from .errors import GitHubHttpError
from .http_client import GitHubHttpClient, GitHubHttpConfig
from .marker_recovery import find_marker_issue, prove_marker_issue

if TYPE_CHECKING:
    from .github_adapter import GitHubAdapter

logger = logging.getLogger(__name__)

# Fallback color/description for labels this adapter provisions in a target
# repo. Concrete tech-lead label metadata is owned by the label manager for the
# managed repo; a foreign repo gets a neutral, self-describing entry.
_PROMOTION_LABEL_COLOR = "ededed"
_PROMOTION_LABEL_DESCRIPTION = "Applied by issue-orchestrator finding promotion"

# GitHub's repository-role matrix, split by the two capabilities filing needs.
# ``triage`` may APPLY an existing label but cannot create, edit, or delete one:
# https://docs.github.com/en/organizations/managing-user-access-to-your-organizations-repositories/managing-repository-roles-for-an-organization#permissions-for-each-role
# That distinction is the whole point of checking the contract rather than the
# repo — provisioning is the first thing ``file_issue`` does (#6957 round-6 F2).
_ISSUE_WRITE_ROLES = ("triage", "push", "maintain", "admin")
_LABEL_WRITE_ROLES = ("push", "maintain", "admin")


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

    def check_filing_ready(self, contract: PromotionFilingContract) -> str | None:
        """None when the token can run the FILING COMMAND, else the reason.

        Proves the whole contract ``file_issue`` consumes, not the weaker "can
        this token open an issue here": provisioning any missing label is the
        first thing filing does, and ``triage`` — an issue-writable role — is
        explicitly not allowed to create labels. A doctor that accepted triage
        approved a route whose first promotion with a missing label dies late,
        which is the exact failure this check exists to prevent (#6957 round-6
        review F2/A1).

        Cost is one repository read per distinct target, plus a label list only
        when the token cannot provision (the case where the gap matters). An
        inconclusive permissions payload fails closed: #6957 requires route
        writability to be PROVEN before startup, not guessed and retried late.
        """
        try:
            return self._filing_problem(contract)
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return (
                    f"{contract.repo} was not found (or the token cannot see it);"
                    " check the route target and the token's repo access"
                )
            return f"{contract.repo} could not be read: {exc}"
        except Exception as exc:  # transport/auth failures
            return f"{contract.repo} could not be read: {exc}"

    def _filing_problem(self, contract: PromotionFilingContract) -> str | None:
        """The reads behind :meth:`check_filing_ready`; raises on read failure."""
        repo = contract.repo
        with self._client_for(repo) as client:
            payload = client.get_repository()
            if not isinstance(payload, dict):
                return f"{repo} returned an unexpected repository payload"
            if payload.get("has_issues") is False:
                return f"{repo} has issues disabled, so findings cannot be filed there"
            permissions = payload.get("permissions")
            if not isinstance(permissions, dict):
                return (
                    f"{repo} did not report effective repository permissions; cannot"
                    " prove the token has issue-write access"
                )
            if not any(permissions.get(role) for role in _ISSUE_WRITE_ROLES):
                return (
                    f"{repo} is readable but not writable by this token; finding"
                    " promotion needs issue-write access"
                )
            if any(permissions.get(role) for role in _LABEL_WRITE_ROLES):
                # Provisioning covers every gap, known or not.
                return None
            if contract.provisions_unknown_labels:
                return (
                    f"{repo} owns the catch-all 'default' promotion route, whose"
                    " findings carry an area label that is not knowable until the"
                    " tech lead classifies them — so filing there needs permission"
                    " to CREATE labels, and this token only has the 'triage' role"
                    " (which may apply existing labels but never create them)."
                    " Grant write access, or route each area to an explicit target"
                )
            missing = self._missing_labels(client, labels=contract.labels)
        if missing:
            return (
                f"{repo} is missing the promotion label(s) {', '.join(missing)} and"
                " this token only has the 'triage' role, which may apply existing"
                " labels but cannot create them; filing provisions missing labels"
                " before it creates the issue, so the first promotion would fail."
                " Create the label(s) in the target repo, or grant write access"
            )
        return None

    @staticmethod
    def _missing_labels(
        client: GitHubHttpClient, *, labels: Sequence[str]
    ) -> tuple[str, ...]:
        """Contract labels the target repo does not already have."""
        if not labels:
            return ()
        existing = _existing_label_names(client)
        return tuple(label for label in labels if label.casefold() not in existing)

    def file_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: Sequence[str],
        idempotency_marker: str,
    ) -> FiledIssue:
        """Create or recover the promotion issue, provisioning labels first.

        Provisioning precedes creation for the same reason the tech-lead issue
        creation boundary does it: GitHub silently DROPS unknown labels, and a
        promotion whose gate label was dropped would be an immediately
        schedulable issue nobody approved.

        The marker lookup here is a best-effort safety net, NOT the at-most-once
        guarantee. That guarantee belongs to ``PromotionFilingOwner``, which
        resolves any pre-existing creation intent through the authoritative
        :meth:`find_filed_issue` before this is ever reached — so by the time a
        create happens, absence has been proven or no create was ever attempted
        for this signature (#6957 round-5 review F13).
        """
        validate_promotion_issue_marker(body=body, marker=idempotency_marker)
        with self._client_for(repo) as client:
            existing = self._find_marker_issue(
                client,
                title=title,
                marker=idempotency_marker,
            )
            if existing is not None:
                return existing
            self._ensure_labels(client, repo=repo, labels=labels)
            result = client.create_issue(title=title, body=body, labels=list(labels))
        if not isinstance(result, dict) or not result.get("number"):
            raise GitHubHttpError(
                f"filing a promoted finding in {repo} returned no issue number"
            )
        return FiledIssue(
            number=int(result["number"]),
            url=str(result.get("html_url") or ""),
            recovered=False,
        )

    def find_filed_issue(
        self, *, repo: str, title: str, idempotency_marker: str
    ) -> FiledIssue | None:
        """The marker-owned issue in *repo*, or None when PROVEN absent.

        The recovery half of :meth:`file_issue`, exposed on its own so the
        filing owner can ask whether an interrupted filing left an issue behind
        in a repo the route no longer points at — without risking a create there
        (#6957 round-4 review F12/A5).

        Its negative answer is load-bearing: the owner retires a durable
        creation intent on it, and retiring one wrongly files a SECOND issue for
        a signature that already has one. So this uses the authoritative,
        title-independent, fail-loud scan — never the bounded best-effort one
        (#6957 round-5 review F13). ``title`` is accepted for interface symmetry
        and deliberately unused: an edited title must not hide the issue.
        """
        if not idempotency_marker.strip():
            raise ValueError("recovering a promotion filing needs its marker")
        with self._client_for(repo) as client:
            found = prove_marker_issue(client, marker=idempotency_marker)
        return (
            FiledIssue(number=found.number, url=found.url, recovered=True)
            if found
            else None
        )

    @staticmethod
    def _find_marker_issue(
        client: GitHubHttpClient, *, title: str, marker: str
    ) -> FiledIssue | None:
        """Find a prior marker-owned filing without trusting cached/search-only data.

        Delegates to the shared :mod:`marker_recovery` policy, which the source
        repo's case-file boundary uses for the identical crash window — one
        recovery rule for both, so they cannot drift.
        """
        found = find_marker_issue(client, title=title, marker=marker)
        return (
            FiledIssue(number=found.number, url=found.url, recovered=True)
            if found
            else None
        )

    @staticmethod
    def _ensure_labels(
        client: GitHubHttpClient, *, repo: str, labels: Sequence[str]
    ) -> None:
        existing = set(_existing_label_names(client))
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
        (the closing-event lookup) only once it has closed, so following a
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
        """URL of the MERGED pull request that CLOSED the issue, else "".

        Merged-ness is the whole distinction the loop closure turns on: closed
        by a merged PR is a shipped fix; closed any other way is the operator
        declining. Both halves of that sentence have to be proven, so this asks
        GitHub which PR actually closed the issue rather than which PRs mention
        its number: a merged PR that merely writes ``#501`` in its body is not
        evidence that #501 was fixed, and treating it as such would write
        shipped-fix memory and permanently close the source case file off an
        unrelated merge (#6957 review F4).

        A read failure raises — it must NOT degrade to "" (which classifies as
        declined). The caller's read wrapper treats a raised error as "unknown
        this tick" and leaves the promotion in flight instead.
        """
        closer = client.get_closing_pull_request(issue_number)
        if not isinstance(closer, dict) or not closer.get("merged"):
            return ""
        url = closer.get("url")
        return str(url) if isinstance(url, str) else ""


def _existing_label_names(client: GitHubHttpClient) -> frozenset[str]:
    """Case-folded names of every label the target repo already has.

    Shared by the readiness probe and the filing-time provisioner so "does this
    label exist" cannot mean two different things on the two paths.
    """
    return frozenset(
        name.casefold()
        for entry in client.list_all_labels()
        if isinstance(entry, dict) and isinstance((name := entry.get("name")), str)
    )


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
