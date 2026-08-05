"""Atomic-write tempfile sweep for the review-exchange ``summary.json``.

This module previously hosted the spawn-per-phase exchange runner that
``CompletionReviewExchange`` dispatched to. The persistent-session
runner in ``execution.persistent_session_exchange`` replaced that loop,
and its tests + integration coverage replaced this module's test
surface. The shared protocol types/builders/parsers live in
``domain.review_exchange``; the atomic-write helper lives in
``infra.atomic_io``. What's left here is the orchestrator-startup
sweep that cleans up tempfiles orphaned by a hard ``kill -9``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..infra.atomic_io import (
    ATOMIC_WRITE_TMP_PREFIX as _ATOMIC_WRITE_TMP_PREFIX,
    ATOMIC_WRITE_TMP_SUFFIX as _ATOMIC_WRITE_TMP_SUFFIX,
)

logger = logging.getLogger(__name__)


def sweep_atomic_write_tempfiles(exchange_dirs_root: Path) -> int:
    """Remove orphaned ``atomic_write_json`` tempfiles under *exchange_dirs_root*.

    ``atomic_write_json`` normally self-cleans in both success (rename) and
    failure (explicit unlink) paths. The one case where it can't: an
    external ``kill -9`` between ``mkstemp`` and ``os.replace``. Those
    tempfiles accumulate silently in per-run ``review-exchange/``
    directories. Runs once at orchestrator startup; O(tempfiles found),
    not O(all files). Returns the number of tempfiles removed.
    """
    if not exchange_dirs_root.exists():
        return 0
    removed = 0
    for tmp_path in exchange_dirs_root.rglob(
        f"{_ATOMIC_WRITE_TMP_PREFIX}*{_ATOMIC_WRITE_TMP_SUFFIX}"
    ):
        # Belt and suspenders: only touch files whose surrounding dir
        # looks like a review-exchange run dir. Prevents an overly-broad
        # root from nuking unrelated dotfiles.
        if tmp_path.parent.name != "review-exchange":
            continue
        try:
            tmp_path.unlink()
            removed += 1
        except OSError:
            # Next startup will retry; don't block boot on sweep failures.
            continue
    return removed


def sweep_orphan_session_tempfiles(repo_root: Path) -> int:
    """Startup sweep of orphaned atomic-write tempfiles under *repo_root*.

    Owns the whole startup concern — where the session run directories live,
    swallowing sweep failures so a boot is never blocked by housekeeping, and
    reporting what was reclaimed — so the orchestrator only has to say "sweep".
    Returns the number of tempfiles removed (0 when the sweep failed).
    """
    sessions_root = repo_root / ".issue-orchestrator" / "sessions"
    try:
        removed = sweep_atomic_write_tempfiles(sessions_root)
    except Exception:
        logger.exception("[STARTUP] Orphan tempfile sweep failed under %s", sessions_root)
        return 0
    if removed:
        logger.info(
            "[STARTUP] Removed %d orphaned atomic-write tempfile(s) under %s",
            removed,
            sessions_root,
        )
    return removed
