"""Milestone checks for doctor."""

from __future__ import annotations

import difflib
from typing import Any

from ..types import Check
from ...config import Config
from ....adapters.github.errors import GitHubAuthError
from ....adapters.github.auth import build_github_auth
from ....adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from ....adapters.github.repo import get_repo_from_git, GitRepoError


def _resolve_repo(config: Config) -> str | None:
    if config.repo:
        return config.repo
    try:
        return get_repo_from_git()
    except GitRepoError:
        return None


def _extract_milestone_titles(payload: Any) -> set[str]:
    if not isinstance(payload, list):
        return set()
    titles: set[str] = set()
    for item in payload:
        if isinstance(item, dict):
            title = item.get("title")
            if isinstance(title, str):
                titles.add(title)
    return titles


def _suggest_title(target: str, titles: set[str]) -> str | None:
    """Return the existing milestone title most likely meant by *target*.

    Prefix matches are checked before fuzzy ones because the realistic mistake
    is a descriptive suffix: configuring ``M0`` against a milestone actually
    titled ``M0 - Foundation``.
    """
    prefix = sorted(
        (t for t in titles if t.startswith(target) or target.startswith(t)),
        key=len,
    )
    if prefix:
        return prefix[0]
    close = difflib.get_close_matches(target, sorted(titles), n=1, cutoff=0.6)
    return close[0] if close else None


def check_foundation_milestone(config: Config) -> list[Check]:
    """Verify ``milestones.foundation`` names a milestone that actually exists.

    Dependency scoping compares this value by exact string equality::

        if dep_milestone != source_milestone and dep_milestone != foundation_milestone:

    so a near-miss does not fail loudly. It fails *silently and totally*: every
    cross-milestone edge pointing at the intended foundation evaluates as
    CROSS_MILESTONE, and each dependent issue becomes non-runnable with an error
    naming a milestone the user believes they configured. Configuring ``M0``
    against a milestone titled ``M0 - Foundation`` is enough to block an entire
    backlog.

    Skipped entirely when the repo has no milestones, since a repo that does not
    use them cannot be affected.
    """
    foundation = config.foundation_milestone
    if not foundation:
        return []

    repo = _resolve_repo(config)
    if not repo:
        return []

    try:
        auth = build_github_auth(
            **config.github_auth_kwargs(),
            repo=repo,
            api_url=config.github_api_url,
            timeout_seconds=float(config.github_http_timeout_seconds),
        )
    except GitHubAuthError:
        # Auth problems are already reported by the GitHub checks; repeating
        # them here would add noise without adding information.
        return []

    client = GitHubHttpClient(GitHubHttpConfig(
        repo=repo,
        base_url=config.github_api_url,
        timeout_seconds=config.github_http_timeout_seconds,
        auth=auth,
    ))
    try:
        milestones = client.list_milestones(state="open")
    except Exception:
        return []
    finally:
        client.close()

    titles = _extract_milestone_titles(milestones)
    if not titles:
        return []

    if foundation in titles:
        return [Check(
            name="Foundation Milestone",
            status="ok",
            detail=f"'{foundation}' exists; any milestone may depend on it",
        )]

    suggestion = _suggest_title(foundation, titles)
    detail = (
        f"milestones.foundation is '{foundation}', which matches no open "
        f"milestone in {repo}."
    )
    if suggestion:
        detail += f" Did you mean '{suggestion}'?"
    detail += (
        " Dependency scoping compares this by exact string equality, so"
        " cross-milestone dependencies on the intended foundation will be"
        " reported as CROSS_MILESTONE and their issues will not run."
    )

    return [Check(
        name="Foundation Milestone",
        status="warning",
        detail=detail,
        expandable={
            "configured": foundation,
            "existing_milestones": sorted(titles),
            "suggestion": suggestion,
        },
    )]


def check_milestone_order(config: Config) -> list[Check]:
    """Verify milestones.order entries exist in the repo (open milestones)."""
    if not config.milestone_order:
        return []

    repo = _resolve_repo(config)
    if not repo:
        return [Check(
            name="Milestone Order",
            status="error",
            detail="Cannot validate milestones.order without a repository",
        )]

    try:
        auth = build_github_auth(
            **config.github_auth_kwargs(),
            repo=repo,
            api_url=config.github_api_url,
            timeout_seconds=float(config.github_http_timeout_seconds),
        )
    except GitHubAuthError as exc:
        return [Check(
            name="Milestone Order",
            status="error",
            detail=str(exc),
        )]

    client = GitHubHttpClient(GitHubHttpConfig(
        repo=repo,
        base_url=config.github_api_url,
        timeout_seconds=config.github_http_timeout_seconds,
        auth=auth,
    ))
    try:
        milestones = client.list_milestones(state="open")
    except Exception as exc:
        return [Check(
            name="Milestone Order",
            status="error",
            detail=f"Failed to list milestones: {exc}",
        )]
    finally:
        client.close()

    titles = _extract_milestone_titles(milestones)
    missing = [name for name in config.milestone_order if name not in titles]
    if missing:
        missing_display = ", ".join(missing)
        return [Check(
            name="Milestone Order",
            status="error",
            detail=f"Missing milestones: {missing_display}",
        )]

    return [Check(
        name="Milestone Order",
        status="ok",
        detail="All ordered milestones found",
    )]
