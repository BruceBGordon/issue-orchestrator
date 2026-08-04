"""Adapter-level GitHub cache coordination helpers."""

from __future__ import annotations

import logging
import re
from typing import Any

from ...ports.pull_request_tracker import PRInfo
from .cache import GitHubCache

logger = logging.getLogger(__name__)


class GitHubAdapterCacheSupport:
    """Own adapter-specific cache rules around the low-level GitHubCache."""

    def __init__(self, cache: GitHubCache, *, label_cache_enabled: bool) -> None:
        self._cache = cache
        self._label_cache_enabled = label_cache_enabled

    def get_cached_labels(self, issue_number: int) -> list[str] | None:
        """Get cached labels for an issue, or None if not cached/stale."""
        if not self._label_cache_enabled:
            return None
        return self._cache.get_issue_labels(issue_number)

    def update_label_cache(self, issue_number: int, labels: list[str]) -> None:
        """Refresh the cached *issue* labels for an issue.

        This deliberately does NOT touch cached PR labels. Issue labels and PR
        labels are distinct facts with separate owners: cached PR labels come
        only from PR reads (``cache_pr_info``) and are invalidated on a
        PR-number label write (``invalidate_pr``). Mirroring issue labels onto a
        cached PR here would corrupt PR-scoped review state — an issue-label
        refresh commonly yields ``[]``, which would erase a still-current
        ``code-reviewed`` from the cached PR and make the stack predecessor
        work-gate read ``agent_reviewed=False`` for a PR that is still reviewed
        (#6595/#6670 F1). Issue-label and PR-label freshness stay separate.
        """
        if not self._label_cache_enabled:
            return
        self._cache.set_issue_labels(issue_number, list(labels))

    def invalidate_label_cache(self, issue_number: int) -> None:
        """Invalidate cached labels for an issue."""
        self._cache.invalidate_issue_labels(issue_number)
        logger.debug("Invalidated label cache for issue %d", issue_number)

    def invalidate_pr_cache(
        self,
        *,
        pr_number: int | None = None,
        issue_number: int | None = None,
        branch: str | None = None,
    ) -> None:
        """Invalidate cached PR info by issue number and/or branch."""
        if pr_number is not None:
            self._cache.invalidate_pr(pr_number)
            logger.debug("Invalidated PR cache for PR %d", pr_number)
        if issue_number is not None:
            self._cache.invalidate_pr_by_issue(issue_number)
            logger.debug("Invalidated PR cache for issue %d", issue_number)
        if branch is not None:
            self._cache.invalidate_pr_by_branch(branch)
            logger.debug("Invalidated PR cache for branch %s", branch)

    def get_cached_pr_for_branch(self, branch: str, state: str) -> PRInfo | None:
        """Return cached PR info for a branch when it satisfies the requested state."""
        cached = self._cache.get_pr_by_branch(branch)
        if not cached:
            return None
        pr_info = self._pr_info_from_cache(cached)
        if pr_info and self._state_matches(pr_info, state):
            return pr_info
        return None

    def get_cached_pr_for_issue(self, issue_number: int, state: str) -> PRInfo | None:
        """Return cached PR info for an issue when one cached PR is a valid answer.

        The by-issue cache holds at most one PR per issue. That single entry can
        prove a PR in a *specific* state exists, but it cannot prove it is the
        *complete* set of an issue's PRs. ``get_prs_for_issue(state="all")``
        callers depend on completeness — the awaiting-merge reconciler suppresses
        ``blocked:pr-closed`` only after confirming no associated PR is open, and
        snapshot building picks a primary PR by preferring open > merged > closed.
        Answering ``all`` from one cached PR could hide a newer open PR behind an
        older closed one, so this owner refuses the cache for ``all`` and lets the
        adapter fetch the authoritative list.
        """
        if state == "all":
            return None
        cached = self._cache.get_pr_by_issue(issue_number)
        if not cached:
            return None
        pr_info = self._pr_info_from_cache(cached)
        if pr_info and self._state_matches(pr_info, state):
            return pr_info
        return None

    def cache_pr_info(self, pr_info: PRInfo) -> None:
        """Cache PR info by issue number when possible, otherwise by branch."""
        issue_number = self._extract_issue_number(pr_info.branch, pr_info.title)
        pr_data = {
            "number": pr_info.number,
            "branch": pr_info.branch,
            "title": pr_info.title,
            "labels": list(pr_info.labels) if pr_info.labels else [],
            "url": pr_info.url,
            "body": pr_info.body,
            "state": pr_info.state,
            "base_branch": pr_info.base_branch,
            "merged_at": pr_info.merged_at,
            "issue_number": issue_number,
        }
        if issue_number is not None:
            self._cache.set_pr_by_issue(issue_number, pr_data, branch=pr_info.branch)
        elif pr_info.branch:
            self._cache.set_pr_by_branch(pr_info.branch, pr_data)

    @staticmethod
    def _state_matches(pr_info: PRInfo, state: str) -> bool:
        return state == "all" or pr_info.state.lower() == state.lower()

    @staticmethod
    def _pr_info_from_cache(cached: dict[str, Any]) -> PRInfo | None:
        """Convert cached PR data back to PRInfo."""
        if not cached:
            return None
        return PRInfo(
            number=cached.get("number", 0),
            title=cached.get("title", ""),
            url=cached.get("url", ""),
            branch=cached.get("branch", ""),
            body=cached.get("body", ""),
            state=cached.get("state", "open"),
            labels=cached.get("labels", []),
            base_branch=cached.get("base_branch"),
            merged_at=cached.get("merged_at"),
        )

    @staticmethod
    def _extract_issue_number(branch: str | None, title: str | None) -> int | None:
        if branch:
            match = re.match(r"^(\d+)-", branch)
            if match:
                return int(match.group(1))
        if title:
            match = re.match(r"^#(\d+):", title)
            if match:
                return int(match.group(1))
        return None
