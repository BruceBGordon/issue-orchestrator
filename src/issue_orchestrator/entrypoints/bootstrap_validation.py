"""Fail-fast validation helpers used by the composition root."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar

from .bootstrap_pending_work import require_repository_host

if TYPE_CHECKING:
    from ..control import LabelSync, Planner, SessionManager
    from ..control.action_applier import ActionApplier
    from ..control.completion_processor import CompletionProcessor
    from ..control.fact_gatherer import FactGatherer
    from ..control.pr_scanner import PRScanner
    from ..control.session_controller import SessionController
    from ..control.session_restorer import SessionRestorer
    from ..events import EventHub
    from ..infra.config import Config
    from ..ports.e2e_issue_tracker import E2EIssueTracker
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.repository_authorization_inspector import (
        RepositoryAuthorizationInspector,
    )
    from ..ports.repository_host import RepositoryHost


logger = logging.getLogger(__name__)
_DependencyT = TypeVar("_DependencyT")


def _require_dependency(
    dependency: _DependencyT | None,
    name: str,
) -> _DependencyT:
    if dependency is None:
        raise ValueError(f"{name} is required")
    return dependency


def validate_required_dependencies(
    github: RepositoryHost | None,
    event_hub: EventHub | None,
    planner: Planner | None,
    session_manager: SessionManager | None,
    label_sync: LabelSync | None,
    action_applier: ActionApplier | None,
    fact_gatherer: FactGatherer | None,
    pr_scanner: PRScanner | None,
    session_restorer: SessionRestorer | None,
    completion_processor: CompletionProcessor | None,
    session_controller: SessionController | None,
    fresh_issue_reader: FreshIssueReader | None,
    e2e_issue_tracker: E2EIssueTracker | None,
) -> None:
    """Reject an incomplete explicitly injected test composition."""
    require_repository_host(github)
    _require_dependency(event_hub, "EventHub")
    _require_dependency(planner, "Planner")
    _require_dependency(session_manager, "SessionManager")
    _require_dependency(label_sync, "LabelSync")
    _require_dependency(action_applier, "ActionApplier")
    _require_dependency(fact_gatherer, "FactGatherer")
    _require_dependency(pr_scanner, "PRScanner")
    _require_dependency(session_restorer, "SessionRestorer")
    _require_dependency(completion_processor, "CompletionProcessor")
    _require_dependency(session_controller, "SessionController")
    _require_dependency(fresh_issue_reader, "FreshIssueReader")
    _require_dependency(e2e_issue_tracker, "E2EIssueTracker")


def check_github_token_scopes(
    config: Config,
    github: RepositoryAuthorizationInspector,
) -> None:
    """Enforce configured OAuth scope bounds when token scopes are available."""
    if github.auth_kind == "github_app":
        logger.info("Skipping OAuth scope check for GitHub App installation auth")
        return
    required = {
        scope.strip()
        for scope in (config.github_required_scopes or [])
        if scope.strip()
    }
    allowed = {
        scope.strip() for scope in (config.github_allowed_scopes or []) if scope.strip()
    }
    try:
        scopes = set(github.get_token_scopes())
    except Exception as exc:
        logger.warning("Failed to fetch GitHub token scopes: %s", exc)
        return

    if required and not required.issubset(scopes):
        missing = sorted(required - scopes)
        raise ValueError(f"GitHub token missing required scopes: {missing}")
    if allowed and not scopes.issubset(allowed):
        extra = sorted(scopes - allowed)
        raise ValueError(f"GitHub token has disallowed scopes: {extra}")

    if scopes:
        logger.info("GitHub token scopes: %s", ", ".join(sorted(scopes)))
    else:
        logger.info(
            "GitHub token scopes unavailable (fine-grained token or missing header)"
        )
