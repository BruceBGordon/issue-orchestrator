"""Port for filing and following a promoted finding in its ROUTED repo (#6957).

Finding promotion routes a pattern case file to the repo that owns the fix,
which is frequently NOT the managed repo the orchestrator is bound to. The
existing ``RepositoryHost`` is single-repo by construction (its caches, label
provisioning, and write verification are all bound to ``config.repo``), so
promotion depends on this narrow behavior-level port instead of teaching every
``RepositoryHost`` method a ``repo=`` parameter.

It exposes exactly the four things the lane needs and nothing else, so the
blast radius of "the orchestrator can write to another repo" stays visible:

* :meth:`PromotionTargetHost.check_writable` — startup/doctor validation that a
  configured route target is reachable AND writable. A route the token cannot
  file issues in must fail loudly at startup, never silently at promotion time.
* :meth:`PromotionTargetHost.file_issue` — create the gated promotion issue,
  or recover the same marker-owned issue after a crash, provisioning its labels
  first so GitHub cannot silently drop the gate and leave a schedulable issue
  behind.
* :meth:`PromotionTargetHost.add_comment` — route a later observation onto the
  already-promoted issue instead of filing a second one.
* :meth:`PromotionTargetHost.read_outcome` — the loop-closure read: is the
  promoted issue still open, and if closed, did a merged PR close it?

Promotion FILES issues, full stop: there is deliberately no approve, merge,
label-removal, or close capability on this port. Cross-repo WRITES are limited
to these two (create issue, comment); everything the source repo does to its
own case files goes through its own ``RepositoryHost``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class FiledIssue:
    """The issue a promotion created in its target repo."""

    number: int
    url: str = ""


def validate_promotion_issue_marker(*, body: str, marker: str) -> None:
    """Reject a filing that cannot be recovered after its local ledger write fails."""
    if not marker.strip() or marker not in body:
        raise ValueError(
            "a promotion issue's non-empty idempotency marker must appear in its body"
        )


@dataclass(frozen=True)
class PromotedIssueOutcome:
    """Terminal-state read of a promoted issue in its target repo.

    ``state`` is ``"open"`` or ``"closed"``. ``merged_pr_url`` is non-empty only
    when a MERGED pull request references the issue — that is the evidence the
    loop actually closed with a shipped fix, as opposed to the issue being
    closed as declined/wontfix.
    """

    state: str
    merged_pr_url: str = ""

    @property
    def closed(self) -> bool:
        return self.state == "closed"


class PromotionTargetHost(Protocol):
    """Cross-repo filing/read seam for the finding-promotion lane."""

    def check_writable(self, *, repo: str) -> str | None:
        """None when *repo* is reachable and issue-writable, else the reason.

        Called by config/doctor validation at startup so a misrouted or
        unauthorized target is a loud configuration error rather than a
        promotion that fails on the tick a pattern finally crosses its
        evidence threshold.
        """
        ...

    def file_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: Sequence[str],
        idempotency_marker: str,
    ) -> FiledIssue:
        """Create or recover one marker-owned gated issue in *repo*.

        Raises on failure — a promotion that cannot be filed must leave the
        ledger untouched so the next tick retries, never record a phantom
        promotion that blocks the signature forever. ``idempotency_marker`` is
        present in ``body`` and must recover an issue created before a crash
        that prevented the local ledger write.
        """
        ...

    def add_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        """Comment on an already-promoted issue in *repo*."""
        ...

    def read_outcome(
        self, *, repo: str, issue_number: int
    ) -> PromotedIssueOutcome | None:
        """Outcome of a promoted issue, or None when it cannot be read.

        None means "unknown this tick" (deleted, unreachable, or a transient
        API failure) — the caller must leave the promotion in flight rather
        than treat an unreadable target as declined.
        """
        ...


class InMemoryPromotionTargetHost:
    """Test double: records filings/comments, replays scripted outcomes."""

    def __init__(
        self,
        *,
        writable: bool = True,
        unwritable_reason: str = "no write access",
    ) -> None:
        self.writable = writable
        self.unwritable_reason = unwritable_reason
        self.filed: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.outcomes: dict[tuple[str, int], PromotedIssueOutcome | None] = {}
        self.next_issue_number = 1000
        self.file_error: Exception | None = None
        self._filed_by_marker: dict[str, FiledIssue] = {}

    def check_writable(self, *, repo: str) -> str | None:
        return None if self.writable else f"{repo}: {self.unwritable_reason}"

    def file_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: Sequence[str],
        idempotency_marker: str,
    ) -> FiledIssue:
        validate_promotion_issue_marker(body=body, marker=idempotency_marker)
        existing = self._filed_by_marker.get(idempotency_marker)
        if existing is not None:
            return existing
        if self.file_error is not None:
            raise self.file_error
        self.filed.append((repo, title, body, tuple(labels)))
        self.next_issue_number += 1
        filed = FiledIssue(
            number=self.next_issue_number,
            url=f"https://example.invalid/{repo}/issues/{self.next_issue_number}",
        )
        self._filed_by_marker[idempotency_marker] = filed
        return filed

    def add_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.comments.append((repo, issue_number, body))

    def read_outcome(
        self, *, repo: str, issue_number: int
    ) -> PromotedIssueOutcome | None:
        return self.outcomes.get(
            (repo, issue_number), PromotedIssueOutcome(state="open")
        )
