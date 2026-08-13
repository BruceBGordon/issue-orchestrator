"""Terminal run-manifest enrichment at the completion boundary."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain.models import Session, SessionStatus
from ..domain.run_manifest import RunManifest
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


def enrich_terminal_run_manifest(
    *,
    session: Session,
    status: SessionStatus,
    run_dir: Path,
    session_output: SessionOutput,
    retention_days: int,
) -> None:
    """Record terminal runtime, retention, and a diagnostic log tail."""
    try:
        manifest = RunManifest.load(run_dir)
    except Exception as exc:
        logger.warning(
            "[MANIFEST] Failed to load manifest for runtime enrichment: %s", exc
        )
        return

    manifest.runtime_minutes = session.runtime_minutes
    if session.agent_config:
        manifest.timeout_minutes = session.agent_config.timeout_minutes
    if manifest.outcome is None:
        manifest.outcome = status.value
    if manifest.ended_at is None:
        manifest.ended_at = datetime.now(timezone.utc).isoformat()

    retention_window_days = max(0, retention_days)
    ended_at = datetime.fromisoformat(manifest.ended_at.replace("Z", "+00:00"))
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
    manifest.retention_days = retention_window_days
    manifest.retention_expires_at = (
        ended_at.astimezone(timezone.utc) + timedelta(days=retention_window_days)
    ).isoformat()
    if manifest.evidence_available is None:
        manifest.evidence_available = True

    log_path = session_output.get_log_path_for_run_dir(run_dir)
    if isinstance(log_path, Path) and log_path.exists():
        try:
            lines = log_path.read_text().strip().split("\n")
            manifest.log_tail = "\n".join(lines[-20:])
        except Exception as exc:
            logger.debug("[MANIFEST] Could not read log tail: %s", exc)

    try:
        manifest.save()
    except Exception as exc:
        logger.warning("[MANIFEST] Failed to save runtime enrichment: %s", exc)
