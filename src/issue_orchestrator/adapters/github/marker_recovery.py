"""Recover an orchestrator-created issue by a body marker (#6957).

Every "create the remote issue, then record the local ledger row" boundary has
the same crash window: the process can die between the two, leaving a real
GitHub issue that no ledger knows about. The next attempt must find that issue
rather than filing a second one — promotion issues are at-most-one per
signature, and so are pattern case files.

This is the ONE recovery policy both boundaries use, so they cannot drift:

* the newest repository issues are read FRESH first, because GitHub's search
  index can lag a successful create by minutes and the crash window is
  precisely "just created";
* title search is the fallback for an older interrupted filing that has fallen
  out of that window;
* ownership is decided by the marker in the BODY, never by a mutable title or
  label, so an edited issue is still recovered and an unrelated issue with a
  similar title never is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .http_client import GitHubHttpClient

# How many of the newest issues to read fresh before falling back to search.
RECENT_ISSUE_WINDOW = 100
TITLE_SEARCH_LIMIT = 30

# Cap for the AUTHORITATIVE walk. Exceeding it raises rather than returning a
# result that cannot be trusted as complete (the ``list_issues(exhaustive=True)``
# contract). 10,000 issues is far beyond any repository this lane files into;
# a repo past it needs a marker-capable index, not a silent wrong answer.
AUTHORITATIVE_SCAN_LIMIT = 10_000


@dataclass(frozen=True)
class MarkerIssue:
    """An existing issue recovered by its body marker."""

    number: int
    url: str = ""


def find_marker_issue(
    client: "GitHubHttpClient", *, title: str, marker: str
) -> MarkerIssue | None:
    """BEST-EFFORT lookup for the issue whose body carries *marker*.

    A hit is authoritative — the marker is in the body, so it is that issue.
    A MISS IS NOT PROOF OF ABSENCE, and no caller may treat it as such: the
    recent window is bounded at 100 and the fallback is a title search
    (``in:title``, capped at 30 results), so an issue that has aged out of the
    window AND had its title edited is invisible here even though its body
    marker is intact (#6957 round-5 review F13).

    Use this only where a miss costs at most a duplicate that some other
    invariant already prevents. Where a negative answer is load-bearing —
    retiring a durable creation intent, deciding a re-routed signature may take
    a new target — use :func:`prove_marker_issue` instead.
    """
    if not marker.strip():
        raise ValueError("marker-based issue recovery requires a non-empty marker")
    recent = client.list_issues(state="all", limit=RECENT_ISSUE_WINDOW, use_cache=False)
    for payload in recent:
        found = _marker_issue(payload, marker)
        if found is not None:
            return found
    for payload in client.search_issues_by_title(
        [title], limit=TITLE_SEARCH_LIMIT, use_cache=False
    ):
        found = _marker_issue(payload, marker)
        if found is not None:
            return found
    return None


def prove_marker_issue(
    client: "GitHubHttpClient",
    *,
    marker: str,
    scan_limit: int = AUTHORITATIVE_SCAN_LIMIT,
) -> MarkerIssue | None:
    """AUTHORITATIVE lookup: ``None`` means PROVEN ABSENT.

    Title-independent by construction — it reads issue BODIES, never a title
    query — and complete or loud, never quietly partial. It reuses the
    repository's one exhaustive-pagination contract
    (``list_issues(exhaustive=True)``, #6779 R8/R17): iteration stops only on a
    true short page, while a later-page failure, a non-200, or exhausting the
    page cap all RAISE.

    So the three outcomes a caller can see are exactly the three that exist: a
    ``MarkerIssue`` (it is there), ``None`` (it is not), or an exception
    (unknown — decide nothing). That is what lets a durable creation intent be
    retired safely: discarding one on a bounded miss files a second issue for a
    signature that already has one (#6957 round-5 review F13).
    """
    if not marker.strip():
        raise ValueError("marker-based issue recovery requires a non-empty marker")
    for payload in client.list_issues(
        state="all", limit=scan_limit, use_cache=False, exhaustive=True
    ):
        found = _marker_issue(payload, marker)
        if found is not None:
            return found
    return None


def _marker_issue(payload: object, marker: str) -> MarkerIssue | None:
    if not isinstance(payload, dict):
        return None
    body = payload.get("body")
    number = payload.get("number")
    if not isinstance(body, str) or marker not in body or not number:
        return None
    return MarkerIssue(number=int(number), url=str(payload.get("html_url") or ""))
