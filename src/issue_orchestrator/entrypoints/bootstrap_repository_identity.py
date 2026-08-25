"""Repository identity resolution used by the composition root."""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..infra.config import Config


logger = logging.getLogger(__name__)


def resolve_repo(
    config: Config,
    detect_from_git: Callable[[], str],
    detection_error: type[Exception],
) -> str | None:
    """Resolve configured repository identity, otherwise detect the git remote."""
    if config.repo:
        return config.repo
    try:
        repo = detect_from_git()
    except detection_error as exc:
        logger.warning("Could not auto-detect repository: %s", exc)
        return None
    logger.info("Auto-detected repository from git remote: %s", repo)
    config.repo = repo
    return repo
