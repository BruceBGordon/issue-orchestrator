"""Composition root shared by repository setup entrypoints."""

from __future__ import annotations

from ..control.repository_setup import RepositorySetupOwner
from ..execution.repository_setup_github_authorization import (
    repository_setup_github_authorization_codec,
)
from ..execution.repository_setup_files import RepositorySetupFileSystemAdapter
from ..ports.repository_setup import (
    RepositorySetupGitHubVerifier,
    RepositorySetupHostFactory,
)
from .setup_wizard_common import plan_setup_labels


def build_repository_setup_owner(
    repository_host_factory: RepositorySetupHostFactory,
    github_verifier: RepositorySetupGitHubVerifier,
) -> RepositorySetupOwner:
    """Wire the one setup owner used by browser and CLI surfaces."""
    return RepositorySetupOwner(
        file_system=RepositorySetupFileSystemAdapter(),
        repository_host_factory=repository_host_factory,
        github_verifier=github_verifier,
        github_authorization_codec=repository_setup_github_authorization_codec,
        label_planner=plan_setup_labels,
    )


__all__ = ["build_repository_setup_owner"]
