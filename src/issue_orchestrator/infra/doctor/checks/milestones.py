"""Milestone checks for doctor.

One owner, one snapshot. Both milestone rules — ``milestones.order`` existence
and ``milestones.foundation`` existence — are negative-existence decisions
("this configured title is not in the repo"), so they must be answered from the
same authoritative read. Two independent fetches would duplicate a rate-limited
call at startup and give completeness and failure policy two places to drift.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from ..types import Check
from ...config import Config
from ....adapters.github.errors import (
    GitHubAuthError,
    GitHubHttpError,
    GitHubTransportError,
)
from ....adapters.github.auth import build_github_auth
from ....adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from ....adapters.github.repo import get_repo_from_git, GitRepoError

# Failures that are expected operating conditions: no token, GitHub down, 5xx.
# Everything else — TypeError, AttributeError, a bad refactor here — is a defect
# in the diagnostic and MUST propagate. A check whose whole purpose is to expose
# a silent configuration failure cannot itself fail silently.
_EXPECTED_GITHUB_ERRORS = (GitHubAuthError, GitHubHttpError, GitHubTransportError)

_ORDER_CHECK = "Milestone Order"
_FOUNDATION_CHECK = "Foundation Milestone"

# Below this, two titles are not plausibly the same intent.
_FUZZY_CUTOFF = 0.6


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


def _suggest_title(target: str, titles: set[str]) -> tuple[str | None, list[str]]:
    """Return ``(suggestion, ambiguous_candidates)`` for *target*.

    A suggestion is only returned when it is unambiguous, because "Did you
    mean X?" against a genuinely ambiguous match invites the user to configure
    the wrong value with confidence. When several titles are equally plausible
    the caller is handed the list instead and presents the ambiguity.

    Prefix matches are considered before fuzzy ones: the realistic mistake is a
    descriptive suffix (``M0`` vs ``M0 - Foundation``), not a misspelling.
    Ordering is by sorted title, never by set iteration order, so the output is
    deterministic across runs.
    """
    prefix = sorted(t for t in titles if t.startswith(target) or target.startswith(t))
    if len(prefix) == 1:
        return prefix[0], []
    if len(prefix) > 1:
        return None, prefix

    scored = [
        (SequenceMatcher(None, target, t).ratio(), t)
        for t in sorted(titles)
    ]
    ranked = sorted(
        ((score, t) for score, t in scored if score >= _FUZZY_CUTOFF),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not ranked:
        return None, []
    if len(ranked) == 1 or ranked[0][0] > ranked[1][0]:
        return ranked[0][1], []
    tied = sorted(t for score, t in ranked if score == ranked[0][0])
    return None, tied


def _evaluate_order(config: Config, titles: set[str]) -> list[Check]:
    missing = [name for name in config.milestone_order if name not in titles]
    if missing:
        return [Check(
            name=_ORDER_CHECK,
            status="error",
            detail=f"Missing milestones: {', '.join(missing)}",
        )]
    return [Check(
        name=_ORDER_CHECK,
        status="ok",
        detail="All ordered milestones found",
    )]


def _evaluate_foundation(foundation: str, titles: set[str], repo: str) -> list[Check]:
    """Warn when ``milestones.foundation`` names a milestone that does not exist.

    Dependency scoping compares this value by exact string equality::

        if dep_milestone != source_milestone and dep_milestone != foundation_milestone:

    so a near-miss does not fail loudly. It fails *silently and totally*: every
    cross-milestone edge pointing at the intended foundation evaluates as
    CROSS_MILESTONE, and each dependent issue becomes non-runnable with an error
    naming a milestone the user believes they configured. Configuring ``M0``
    against a milestone titled ``M0 - Foundation`` is enough to block a backlog.

    Silent when the repo has no milestones: such a repo cannot be affected.
    """
    if not titles:
        return []

    if foundation in titles:
        return [Check(
            name=_FOUNDATION_CHECK,
            status="ok",
            detail=f"'{foundation}' exists; any milestone may depend on it",
        )]

    suggestion, ambiguous = _suggest_title(foundation, titles)
    detail = (
        f"milestones.foundation is '{foundation}', which matches no milestone "
        f"in {repo}."
    )
    if suggestion:
        detail += f" Did you mean '{suggestion}'?"
    elif ambiguous:
        detail += " Closest matches: " + ", ".join(f"'{t}'" for t in ambiguous) + "."
    detail += (
        " Dependency scoping compares this by exact string equality, so"
        " cross-milestone dependencies on the intended foundation will be"
        " reported as CROSS_MILESTONE and their issues will not run."
    )

    return [Check(
        name=_FOUNDATION_CHECK,
        status="warning",
        detail=detail,
        expandable={
            "configured": foundation,
            "existing_milestones": sorted(titles),
            "suggestion": suggestion,
            "ambiguous_candidates": ambiguous,
        },
    )]


def _unavailable(wants_order: bool, foundation: str, reason: str) -> list[Check]:
    """Report that neither rule could be evaluated, rather than staying silent.

    Returning ``[]`` here would be indistinguishable from "configuration is
    fine", which is the failure mode these checks exist to prevent.
    """
    checks: list[Check] = []
    if wants_order:
        checks.append(Check(name=_ORDER_CHECK, status="error", detail=reason))
    if foundation:
        checks.append(Check(
            name=_FOUNDATION_CHECK,
            status="warning",
            detail=f"Could not verify milestones.foundation '{foundation}': {reason}",
        ))
    return checks


def check_milestones(config: Config) -> list[Check]:
    """Evaluate every milestone rule from a single authoritative snapshot."""
    wants_order = bool(config.milestone_order)
    foundation = (config.foundation_milestone or "").strip()
    if not wants_order and not foundation:
        return []

    repo = _resolve_repo(config)
    if not repo:
        return _unavailable(
            wants_order, foundation, "Cannot validate milestones without a repository"
        )

    try:
        auth = build_github_auth(
            **config.github_auth_kwargs(),
            repo=repo,
            api_url=config.github_api_url,
            timeout_seconds=float(config.github_http_timeout_seconds),
        )
    except _EXPECTED_GITHUB_ERRORS as exc:
        return _unavailable(wants_order, foundation, str(exc))

    client = GitHubHttpClient(GitHubHttpConfig(
        repo=repo,
        base_url=config.github_api_url,
        timeout_seconds=config.github_http_timeout_seconds,
        auth=auth,
    ))
    try:
        # All states, exhaustively paged: milestone scope is evaluated before
        # issue state, so a CLOSED milestone is still a valid dependency target,
        # and a title on page 2 is still a title. Either omission would turn a
        # correct configuration into a false report.
        milestones = client.list_all_milestones()
    except _EXPECTED_GITHUB_ERRORS as exc:
        return _unavailable(wants_order, foundation, f"Failed to list milestones: {exc}")
    finally:
        client.close()

    titles = _extract_milestone_titles(milestones)
    checks: list[Check] = []
    if wants_order:
        checks.extend(_evaluate_order(config, titles))
    if foundation:
        checks.extend(_evaluate_foundation(foundation, titles, repo))
    return checks
