"""Terminal run-manifest enrichment at the completion boundary."""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.models import Session
from ..domain.run_manifest import RunManifest
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


def enrich_run_runtime_manifest(
    *,
    session: Session,
    run_dir: Path,
    session_output: SessionOutput,
) -> None:
    """Record runtime context and a diagnostic log tail."""
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
