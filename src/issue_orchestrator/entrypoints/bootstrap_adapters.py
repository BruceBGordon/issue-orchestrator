"""Adapter composition helpers owned by the bootstrap root."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..adapters.github import (
    GitHubAdapter,
    GitHubAuth,
    GitHubCache,
    build_github_auth,
)
from ..execution.command_runner import LocalCommandRunner
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..execution.verification_service import DefaultVerificationService
from ..execution.worktree_adapter import GitWorktreeManager
from ..infra import gh_audit
from ..infra.config import Config
from ..ports.event_sink import EventSink
from ..ports.verification import VerificationBudget

if TYPE_CHECKING:
    from ..ports.attempt_store import AttemptStore

logger = logging.getLogger(__name__)


def create_github_auth(repo: str, config: Config) -> GitHubAuth:
    """Create the shared GitHub auth owner for API and git transport."""
    return build_github_auth(
        **config.github_auth_kwargs(),
        repo=repo,
        api_url=config.github_api_url,
        timeout_seconds=float(config.github_http_timeout_seconds),
    )


def create_github_adapter(
    repo: str, config: Config, auth: GitHubAuth
) -> GitHubAdapter:
    """Create GitHub adapter with cache and verification service."""
    cache_ttl = float(max(0, getattr(config, "fetch_layer_network_sync_seconds", 0)))
    github_cache = GitHubCache(default_ttl=cache_ttl)

    default_budget = VerificationBudget(
        timeout_seconds=config.gh_write_verify_timeout_seconds,
        max_attempts=20,
        initial_delay_ms=config.gh_write_verify_initial_delay_ms,
        max_delay_ms=config.gh_write_verify_max_delay_ms,
        backoff_factor=config.gh_write_verify_backoff,
        jitter_ms=config.gh_write_verify_jitter_ms,
    )
    verification_service = DefaultVerificationService(default_budget=default_budget)

    return GitHubAdapter(
        repo,
        config=config,
        cache=github_cache,
        verification_service=verification_service,
        auth=auth,
    )


def configure_gh_audit(
    config: Config,
    events: EventSink,
    github: GitHubAdapter | None,
) -> None:
    """Configure GitHub audit logging."""
    gh_audit.set_event_sink(events)
    if github:
        gh_audit.set_rate_limit_fetcher(github.get_rate_limit_snapshot)
    gh_audit.configure(
        enabled=config.gh_audit_enabled,
        include_events=config.gh_audit_events,
        audit_path=config.gh_audit_file,
    )
    gh_audit.configure_rate_limit(
        every_calls=config.gh_rate_limit_every_calls,
        warn_fraction=config.gh_rate_limit_warn_fraction,
        warn_remaining=config.gh_rate_limit_warn_remaining,
    )
    if config.gh_rate_limit_startup:
        rl_start = time.time()
        gh_audit.check_rate_limit("startup")
        logger.info(
            "[STARTUP_TIMING] phase=gh_rate_limit_probe elapsed=%.3fs",
            time.time() - rl_start,
        )


def create_io_adapters(
    github_auth: GitHubAuth | None = None,
) -> tuple[
    GitWorktreeManager,
    GitWorkingCopy,
    LocalCommandRunner,
    FileSystemSessionOutput,
]:
    """Create IO adapter instances."""
    return (
        GitWorktreeManager(),
        GitWorkingCopy(git_auth=github_auth),
        LocalCommandRunner(),
        FileSystemSessionOutput(),
    )


def create_attempt_store(config: Config) -> "AttemptStore":
    """Create the attempt store for this repository."""
    from ..adapters.sidecar_attempt_store import SidecarAttemptStore

    return SidecarAttemptStore(config.repo_root)
