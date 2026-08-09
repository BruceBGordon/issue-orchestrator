"""Port for preserving a tech-lead run's inspectable artifacts (#6858 F4).

ADR-0033 puts the session log, the evidence map, the decision and the proposals
on the winning engine's dashboard. The run that produces them writes them inside
its worktree — and for a failure investigation that worktree is DISPOSABLE
scratch which normal completion always removes (#6823). So the artifacts have to
be moved out of the run's own space before the run's own space is deleted, and
the thing that moves them is an engine-owned archive rather than the cleanup
path being taught to make exceptions.

The archive is a port for the same reason the record store is: preserving files
is filesystem work, the owner that decides WHEN to preserve them is control-layer
policy, and a test wants the second without the first.

Exception contract, mirroring the record store's: implementations MUST NOT raise.
A run whose receipt cannot be filed is still a run that happened; the archive
therefore returns ``None`` and logs, and the record simply carries no drill-down.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Protocol

from ..domain.tech_lead_run_artifacts import TechLeadRunArtifacts

logger = logging.getLogger(__name__)


class TechLeadRunArtifactArchive(Protocol):
    """Durable home for the artifacts of runs this engine has finished."""

    def preserve(
        self,
        *,
        run_id: str,
        session_name: str,
        run_dir: Path,
    ) -> Optional[TechLeadRunArtifacts]:
        """Copy ``run_dir``'s inspectable artifacts somewhere nothing deletes.

        Returns the typed locator for what was preserved, or ``None`` when there
        was nothing to preserve (the run wrote no artifacts) or preserving it
        failed. Never raises: see the module docstring.

        Idempotent on the session run identity — re-preserving one run replaces
        its archive rather than accumulating copies, so a publish retry that
        re-enters completion does not multiply the run's evidence.
        """
        ...


class DiscardedTechLeadRunArtifacts:
    """The archive of a composition that has no durable home for artifacts.

    An EXPLICIT choice, not a fallback: a bounded test or a one-shot CLI selects
    this so it is obvious in the composition that this process keeps no evidence,
    rather than discovering it when a drill-down turns out to be missing (#6858
    round 1 A2).
    """

    def preserve(
        self,
        *,
        run_id: str,
        session_name: str,
        run_dir: Path,
    ) -> Optional[TechLeadRunArtifacts]:
        logger.debug(
            "[TECH_LEAD_RUN] No artifact archive is wired; %s/%s keeps no"
            " inspectable evidence of %s",
            run_id,
            session_name,
            run_dir,
        )
        return None


__all__ = [
    "DiscardedTechLeadRunArtifacts",
    "TechLeadRunArtifactArchive",
]
