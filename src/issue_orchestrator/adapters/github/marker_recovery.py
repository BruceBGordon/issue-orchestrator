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


@dataclass(frozen=True)
class MarkerIssue:
    """An existing issue recovered by its body marker."""

    number: int
    url: str = ""


def find_marker_issue(
    client: "GitHubHttpClient", *, title: str, marker: str
) -> MarkerIssue | None:
    """The issue whose body carries *marker*, or None when none exists."""
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


def _marker_issue(payload: object, marker: str) -> MarkerIssue | None:
    if not isinstance(payload, dict):
        return None
    body = payload.get("body")
    number = payload.get("number")
    if not isinstance(body, str) or marker not in body or not number:
        return None
    return MarkerIssue(number=int(number), url=str(payload.get("html_url") or ""))
