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

The archive owns more than the copy, because the source is agent-authored and the
destination is the operator's state volume (#6858 round 2 F6/A3). One owner is
responsible for all three of:

* **safe admission** — only real, contained, bounded regular files are preserved;
* **atomic publication** — a run's archive is replaced only by a COMPLETE new
  one, so a failed retry cannot destroy the receipt that was already there;
* **bounded retention** — :meth:`TechLeadRunArtifactArchive.prune` reports what it
  removed so the caller can retire the matching record locators in the same
  breath. Splitting retention from the locator update is what leaves live rows
  advertising directories that are gone.

Exception contract, mirroring the record store's: implementations MUST NOT raise.
A run whose receipt cannot be filed is still a run that happened; the archive
therefore returns ``None`` and logs, and the record simply carries no drill-down.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Protocol

from ..domain.tech_lead_run_artifacts import TechLeadRunArtifacts, TechLeadRunSource

logger = logging.getLogger(__name__)


class TechLeadRunArtifactArchive(Protocol):
    """Durable home for the artifacts of runs this engine has finished."""

    def preserve(
        self, *, run: TechLeadRunSource
    ) -> Optional[TechLeadRunArtifacts]:
        """Copy a run's inspectable artifacts somewhere nothing deletes.

        Takes the typed source rather than a run id, a session name and a naked
        path: safe admission cannot be promised by an implementation that is only
        told WHERE to read, because the trust boundary is the relationship between
        the engine-created worktree and the agent-writable components below it
        (#6858 round 5 F16/A5).

        Returns the typed locator for what was preserved, or ``None`` when there
        was nothing to preserve (the run wrote no artifacts) or preserving it
        failed. Never raises: see the module docstring.

        Idempotent on the session run identity — re-preserving one run replaces
        its archive rather than accumulating copies, so a publish retry that
        re-enters completion does not multiply the run's evidence. The
        replacement is atomic: a failed attempt leaves the previously preserved
        archive exactly as it was.
        """
        ...

    def prune(self) -> tuple[Path, ...]:
        """Apply retention and return the locations that no longer exist.

        Called by the activity owner right after a conclusion, which then retires
        the record locators pointing at those locations — so retention and the
        rows that advertise it move together. Never raises: a retention pass that
        cannot run leaves a bigger archive, not a failed run.
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
        self, *, run: TechLeadRunSource
    ) -> Optional[TechLeadRunArtifacts]:
        logger.debug(
            "[TECH_LEAD_RUN] No artifact archive is wired; %s/%s keeps no"
            " inspectable evidence of %s",
            run.run_id,
            run.session_name,
            run.run_dir,
        )
        return None

    def prune(self) -> tuple[Path, ...]:
        """Nothing was ever kept, so nothing is ever retired."""
        return ()


__all__ = [
    "DiscardedTechLeadRunArtifacts",
    "TechLeadRunArtifactArchive",
]
