"""Platform prerequisite checks for host-executor composition."""

from __future__ import annotations

import os


def require_posix_executor() -> None:
    """Reject pooled execution where POSIX advisory locks are unavailable."""
    if os.name != "posix":
        raise RuntimeError(
            "the pooled host executor requires POSIX advisory locks; "
            "use executor-run-direct explicitly for unpooled execution"
        )


def raise_missing_posix_executor_dependency(exc: ModuleNotFoundError) -> None:
    """Translate known missing POSIX modules without hiding other import defects."""
    if exc.name not in {"fcntl", "resource"}:
        raise exc
    raise RuntimeError(
        "the pooled host executor requires POSIX fcntl and resource support; "
        "use executor-run-direct explicitly for unpooled execution"
    ) from exc
