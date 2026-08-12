"""Shared support helpers for CLI command handlers."""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from ..infra.client_urls import resolve_client_dashboard_url, with_client_query_params

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports import RepositoryHost

console = Console()
logger = logging.getLogger(__name__)

__all__ = [
    "client_dashboard_link",
    "get_repository_host",
    "load_config",
    "resolve_repo",
    "run_test_setup",
]


def client_dashboard_link(port: int, *, repo_path: str | None = None) -> str:
    """Build a browser-usable dashboard URL for local or Codespaces clients."""
    return with_client_query_params(resolve_client_dashboard_url(port), repo=repo_path)


def resolve_repo(config: "Config") -> str:
    from ..execution.providers import get_repo_from_git

    repo = config.repo or get_repo_from_git()
    if repo is None:
        raise ValueError(
            "Could not determine repository. Set 'repo' in config or run from a git directory."
        )
    return repo


def get_repository_host(config: "Config") -> "RepositoryHost | None":
    """Get a RepositoryHost for the given config.

    All GitHub access in CLI is routed through the repository host for
    consistent auditing and rate-limit handling.
    """
    from ..execution.providers import create_repository_host

    try:
        repo = resolve_repo(config)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return None
    if not repo:
        console.print("[red]Error: repo must be set in config[/red]")
        return None
    return create_repository_host(repo=repo, config=config)


def _build_action_applier(config: "Config", adapter: "RepositoryHost"):
    from ..control.action_applier import ActionApplier
    from ..control.session_manager import SessionManager
    from ..ports import NullEventSink, NullSessionRunner

    events = NullEventSink()
    sessions = SessionManager(runner=NullSessionRunner(), events=events, config=config)
    return ActionApplier(
        labels=adapter,
        sessions=sessions,
        events=events,
        repository_host=adapter,
    )


def run_test_setup(config: "Config") -> bool:  # noqa: C901 - inherent complexity from multi-step setup with graceful error handling
    """Run test teardown and setup. Returns True on success."""
    adapter = get_repository_host(config)
    if adapter is None:
        return False
    action_applier = _build_action_applier(config, adapter)
    try:
        repo = resolve_repo(config)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return False

    console.print("[cyan]Test mode: Cleaning up old test issues...[/cyan]")

    try:
        from ..control.actions import AddCommentAction

        # Adapter returns list[Issue] with .number attribute
        issues = adapter.list_issues(labels=["test-data"], state="open", limit=100)
        for issue in issues:
            result = action_applier.apply(
                AddCommentAction(
                    number=issue.number,
                    comment="Closed by test mode startup.",
                    reason="test mode cleanup",
                )
            )
            if not result.success:
                logger.warning(
                    "Failed to add test cleanup comment for #%d: %s",
                    issue.number,
                    result.error or "unknown error",
                )
            adapter.update_issue_state(issue.number, "closed")
            console.print(f"  Closed #{issue.number}")
    except Exception as exc:
        logger.warning("Test setup cleanup failed: %s", exc)

    console.print("[cyan]Test mode: Creating fresh test issues...[/cyan]")

    # Create test-data label if missing
    try:
        adapter.create_label(
            "test-data",
            description="Test data for integration tests",
            force=True,
        )
    except Exception as exc:
        logger.warning("Failed to ensure test-data label: %s", exc)

    # Create 5 test issues (matches scripts/setup_test_issues.py)
    test_issues = [
        ("[TEST] Simple backend task", "agent:backend", "priority:high"),
        ("[TEST] Frontend feature", "agent:frontend", "priority:medium"),
        ("[TEST] Mobile bug fix", "agent:mobile", "priority:low"),
        ("[TEST] Task that will block", "agent:backend", None),
        ("[TEST] Task with dependency", "agent:backend", None),
    ]

    for title, agent_label, priority_label in test_issues:
        labels = ["test-data", agent_label]
        if priority_label:
            labels.append(priority_label)
        try:
            adapter.create_label(agent_label, force=True)
            if priority_label:
                adapter.create_label(priority_label, force=True)
            issue_number = adapter.create_issue(
                title=title,
                body="Test issue for orchestrator.\n\nExpected: Agent completes.",
                labels=labels,
            )
            if issue_number:
                console.print(
                    f"  Created: https://github.com/{repo}/issues/{issue_number}"
                )
        except Exception as exc:
            logger.warning("Failed to create test issue '%s': %s", title, exc)

    return True


def load_config(args: argparse.Namespace) -> "Config":
    """Load config from explicit path or search for it.

    Args:
        args: Parsed command line arguments

    Returns:
        Loaded Config object

    Raises:
        FileNotFoundError: If config file not found
    """
    from ..infra.config import Config

    overrides = getattr(args, "set", None) or []
    if hasattr(args, "config") and args.config:
        config_path = Path(args.config)
        # Config.load() handles repo_root calculation properly
        config = Config.load(config_path, overrides=overrides)
        explicit_mode = getattr(args, "mode", None)
        if explicit_mode and config.configuration_mode != explicit_mode:
            raise ValueError(
                "Explicit --mode does not match --config path: "
                f"mode={explicit_mode!r} path_mode={config.configuration_mode!r}"
            )
        return config
    return Config.find_and_load(
        overrides=overrides,
        mode=getattr(args, "mode", None) or "default",
    )
