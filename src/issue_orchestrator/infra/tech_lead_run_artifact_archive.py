"""Engine-owned durable home for finished tech-lead run artifacts (#6858 F4).

``.issue-orchestrator/state/tech-lead-runs/<run>/`` — beside the databases, and
deliberately NOT inside any worktree. A tech-lead investigation runs in
disposable scratch (#6823) that completion always removes, so anything left in
the run's own directory is gone by the time an operator reads the history. This
adapter copies the run's inspectable set out of that directory while it still
exists, preserving the RUN-RELATIVE LAYOUT so the existing run-scoped readers
(terminal-recording replay, the tech-lead artifact reader) work against the
archive without knowing it is one.

Copies, never moves: completion is still reading the same directory (the audit
writer, the analysis pass), and a move would pull the ground out from under
them. The originals die with their worktree anyway.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from ..domain.tech_lead_run_artifacts import (
    ARTIFACT_KIND_ORDER,
    TECH_LEAD_DATA_DIRNAME,
    TERMINAL_RECORDING_FILENAME,
    TechLeadRunArtifacts,
)
from .repo_identity import state_dir

logger = logging.getLogger(__name__)

# The archive's directory name under the state directory.
ARCHIVE_DIRNAME = "tech-lead-runs"

# What is copied out of a run directory, as run-relative members. The three
# drill-down KINDS live in here, and so does the context that makes them
# readable: the manifest a run-scoped reader resolves against, the prompt the
# run was launched with, and the whole evidence directory (evidence map, board
# snapshot, proposals) the report cites. Preserving the citations alongside the
# report is the difference between evidence and an assertion.
_PRESERVED_FILES: tuple[str, ...] = (
    "manifest.json",
    TERMINAL_RECORDING_FILENAME,
    "session-prompt.txt",
)
_PRESERVED_TREES: tuple[str, ...] = (TECH_LEAD_DATA_DIRNAME,)


class FileSystemTechLeadRunArtifactArchive:
    """Preserves run artifacts under a directory this engine owns."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @classmethod
    def for_repo(cls, repo_root: Path) -> "FileSystemTechLeadRunArtifactArchive":
        """Archive handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ``TechLeadRunArtifactArchive`` port instead.
        """
        return cls(state_dir(repo_root) / ARCHIVE_DIRNAME)

    def preserve(
        self,
        *,
        run_id: str,
        session_name: str,
        run_dir: Path,
    ) -> Optional[TechLeadRunArtifacts]:
        """Copy one run's inspectable artifacts into the archive.

        Best-effort by contract: every filesystem failure is logged and reported
        as "no artifacts preserved", because a lost receipt must never fail the
        run that earned it.
        """
        destination = self._root / _archive_name(run_id, session_name)
        try:
            members = self._copy_members(run_dir, destination)
        except OSError:
            logger.warning(
                "[TECH_LEAD_RUN] Could not preserve the artifacts of %s/%s"
                " from %s",
                run_id,
                session_name,
                run_dir,
                exc_info=True,
            )
            return None
        if not members:
            # Nothing was there to preserve. Removing the empty directory keeps
            # the archive free of shells that look like preserved runs.
            shutil.rmtree(destination, ignore_errors=True)
            logger.info(
                "[TECH_LEAD_RUN] Run %s/%s wrote no inspectable artifacts to"
                " preserve",
                run_id,
                session_name,
            )
            return None
        kinds = tuple(
            kind
            for kind in ARTIFACT_KIND_ORDER
            if _is_readable_file(destination / Path(kind.member))
        )
        if not kinds:
            logger.info(
                "[TECH_LEAD_RUN] Preserved %d support file(s) for %s/%s but no"
                " inspectable artifact",
                len(members),
                run_id,
                session_name,
            )
            return None
        logger.info(
            "[TECH_LEAD_RUN] Preserved %s for %s/%s at %s",
            ", ".join(kind.value for kind in kinds),
            run_id,
            session_name,
            destination,
        )
        return TechLeadRunArtifacts(location=destination.resolve(), kinds=kinds)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _copy_members(self, run_dir: Path, destination: Path) -> tuple[str, ...]:
        """Replace ``destination`` with the members present in ``run_dir``."""
        # Replaced wholesale so a re-preserve cannot leave half of an older
        # attempt behind and advertise a kind that is no longer complete.
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in _PRESERVED_FILES:
            source = run_dir / name
            if _is_readable_file(source):
                shutil.copy2(source, destination / name)
                copied.append(name)
        for name in _PRESERVED_TREES:
            source = run_dir / name
            if source.is_dir():
                shutil.copytree(source, destination / name, dirs_exist_ok=True)
                copied.append(name)
        return tuple(copied)


def _is_readable_file(path: Path) -> bool:
    """True for a real, non-empty file.

    Empty is treated as absent throughout the run-artifact surfaces: an empty
    recording or a zero-byte decision is a capture gap, and offering a
    drill-down into one only teaches an operator to distrust the buttons.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _archive_name(run_id: str, session_name: str) -> str:
    """A filesystem-safe directory name for one session run.

    Both halves are orchestrator-minted, never agent-authored, but they are
    sanitised anyway: this builds a path, and a component that could contain a
    separator is a traversal waiting for the first caller who forgets.
    """
    return f"{_safe_component(run_id)}__{_safe_component(session_name)}"


def _safe_component(value: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_." else "-" for char in value.strip()
    )
    return safe.strip(".-") or "unnamed"


__all__ = [
    "ARCHIVE_DIRNAME",
    "FileSystemTechLeadRunArtifactArchive",
]
