"""Adopting a terminal that is already running.

A launch can find the work already underway — a terminal survived a restart,
or another path started it. That is not a failure and not a fresh launch: it
is an adoption, with its own discovery, matching and run-directory recovery
rules. Kept together here so the routing layer above reads as queue
ownership rather than as terminal archaeology.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..domain.models import Session
from ..ports.session_runner import DiscoveredSession
from .active_sessions import append_unique_active_sessions
from .session_launcher import SessionLauncher

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from .session_restorer import SessionRestorer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ExistingTerminalRestorationRequest:
    """Typed request to restore one known terminal from runner discovery."""

    issue_number: int
    session_name: str
    is_review: bool
    tab_name: str = ""


def _restore_existing_terminal(
    *,
    request: _ExistingTerminalRestorationRequest,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    discovered = _discover_existing_terminal(
        request=request,
        session_launcher=session_launcher,
        session_restorer=session_restorer,
    )
    if discovered is None:
        _log_unrestorable_existing_terminal(request.session_name)
        return None

    run_dir = _recorded_run_dir_from_discovered(discovered, request.session_name)
    if run_dir is None:
        return None

    restored = session_restorer.restore_known_terminal(
        issue_number=request.issue_number,
        session_name=request.session_name,
        run_dir=run_dir,
        is_review=request.is_review,
        already_tracked=list(state.active_sessions),
        tab_name=request.tab_name,
    )
    added = append_unique_active_sessions(state.active_sessions, restored)
    if not added:
        _log_unrestorable_existing_terminal(request.session_name)
        return None
    logger.info(
        "[ORPHAN] Restored existing terminal %s from discovered run assets: %s",
        request.session_name,
        run_dir,
    )
    return added[0]


def _discover_existing_terminal(
    *,
    request: _ExistingTerminalRestorationRequest,
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> "DiscoveredSession | None":
    try:
        running = session_launcher.session_manager.runner.discover_running_sessions()
    except Exception:
        logger.exception(
            "[ORPHAN] Failed to discover running terminal sessions for %s",
            request.session_name,
        )
        return None

    for raw_session_info in running:
        session_info = _discovered_session_from_raw(raw_session_info)
        if session_info is None:
            continue
        if _matches_existing_terminal(
            session_info=session_info,
            request=request,
            session_restorer=session_restorer,
        ):
            return session_info
    return None


def _discovered_session_from_raw(raw: object) -> DiscoveredSession | None:
    if not isinstance(raw, dict):
        return None

    raw_issue_number = raw.get("issue_number")
    raw_tab_name = raw.get("tab_name")
    raw_is_review = raw.get("is_review")
    raw_run_dir = raw.get("run_dir")
    if isinstance(raw_issue_number, bool) or not isinstance(raw_issue_number, int):
        return None
    if not isinstance(raw_tab_name, str):
        return None
    if not isinstance(raw_is_review, bool):
        return None
    run_dir = raw_run_dir if isinstance(raw_run_dir, str) else ""
    raw_session_name = raw.get("session_name")
    if isinstance(raw_session_name, str):
        return DiscoveredSession(
            issue_number=raw_issue_number,
            tab_name=raw_tab_name,
            is_review=raw_is_review,
            run_dir=run_dir,
            session_name=raw_session_name,
        )
    return DiscoveredSession(
        issue_number=raw_issue_number,
        tab_name=raw_tab_name,
        is_review=raw_is_review,
        run_dir=run_dir,
    )


def _matches_existing_terminal(
    *,
    session_info: "DiscoveredSession",
    request: _ExistingTerminalRestorationRequest,
    session_restorer: "SessionRestorer",
) -> bool:
    discovered_names = {
        str(session_info.get("session_name") or ""),
        str(session_info.get("tab_name") or ""),
    }
    try:
        discovered_names.add(session_restorer.canonical_terminal_id(session_info))
    except Exception:
        logger.debug(
            "[ORPHAN] Could not derive canonical terminal id from discovered session",
            exc_info=True,
        )
    return request.session_name in discovered_names


def _recorded_run_dir_from_discovered(
    session_info: "DiscoveredSession",
    session_name: str,
) -> Path | None:
    raw: object = session_info.get("run_dir")
    if type(raw) is not str or not raw.strip():
        logger.warning(
            "[ORPHAN] Existing terminal %s has no recorded run_dir from runner discovery",
            session_name,
        )
        return None
    run_dir = Path(raw)
    if not run_dir.is_absolute():
        logger.warning(
            "[ORPHAN] Existing terminal %s reported non-absolute run_dir: %s",
            session_name,
            run_dir,
        )
        return None
    return run_dir


def _log_unrestorable_existing_terminal(session_name: str) -> None:
    logger.warning(
        "[ORPHAN] Existing terminal %s cannot be restored from launch routing; "
        "active restoration requires discovered run assets",
        session_name,
    )


__all__ = ["_ExistingTerminalRestorationRequest", "_restore_existing_terminal"]
