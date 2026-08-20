"""Engine-owned durable home for finished tech-lead run artifacts (#6858 F4/F6/F8-F10).

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

The source is AGENT-AUTHORED and the destination is the operator's state volume,
so this is a bounded owner rather than a copy loop. Four properties, each of
which was a real hole in an earlier round:

* **Race-free admission (#6858 round 3 F9).** The walk is anchored on an open
  descriptor for the run directory and every component is opened with
  ``O_NOFOLLOW`` relative to its parent's descriptor, exactly as
  :mod:`...control.validation_record_containment` does for agent-supplied
  validation records. Nothing is ever reopened by pathname, so a descendant that
  swaps a file — or an ancestor directory — between the check and the copy cannot
  make the archive read from outside the run. Bytes are streamed from that
  descriptor under a hard ceiling, so a file that GROWS after its ``fstat``
  cannot spend more budget than it was admitted for.
* **Bounded discovery (#6858 round 3 F8).** Walking is iterative (no recursion to
  exhaust), lazy (an unbounded directory is never materialised into a list), and
  capped on entries visited, directories entered, and depth. A per-entry failure
  refuses that entry only; it never costs the artifacts already admitted.
* **Bounds on what lands.** A per-file cap — 2 MiB for agent-authored artifacts,
  the same gate the decision loader applies before parsing one, and a separate
  larger cap for the orchestrator-written PTY capture — plus aggregate byte and
  file-count budgets. Everything dropped is logged: a silent cap reads as "we
  preserved it all".
* **Crash-safe publication + bounded retention (#6858 round 3 F10).** A copy is
  staged in a PID-owned sibling directory and swapped in only once complete.
  ``reconcile()`` restores a receipt that a crash left renamed aside, and
  reclaims scratch belonging to processes that are gone — never a live engine's
  active stage. ``prune()`` then keeps the newest ``ARCHIVE_RETENTION`` runs and
  REPORTS what it removed, so the caller can retire the matching record locators
  in the same breath.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..domain.tech_lead_run_artifacts import (
    ARTIFACT_KIND_ORDER,
    TECH_LEAD_DATA_DIRNAME,
    TERMINAL_RECORDING_FILENAME,
    TechLeadRunArtifactKind,
    TechLeadRunArtifacts,
    TechLeadRunSource,
)
from .contained_artifact_copy import (
    CopyBounds,
    CopyBudget,
    close_fd,
    copy_contained_file,
    copy_contained_tree,
    open_contained_anchor,
    unlink,
)
from .repo_identity import state_dir
from .shutdown_timing import process_is_alive

logger = logging.getLogger(__name__)

# The archive's directory name under the state directory.
ARCHIVE_DIRNAME = "tech-lead-runs"

# Scratch prefixes. Dot-prefixed so a half-written or being-swapped directory is
# never mistaken for a preserved run by retention or by anything listing the
# archive. A staging name also carries the OWNING PID, so reconciliation can tell
# an abandoned stage from another live engine's work in progress.
_STAGING_PREFIX = ".incoming-"
_RETIRED_PREFIX = ".retired-"

# Per-file cap for agent-authored artifacts: the same 2 MiB the decision loader
# applies before it will parse one. A file over this is not a receipt, it is a
# blob, and copying it into the operator's state volume is the failure mode.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
# The terminal recording is orchestrator-written PTY capture, and a real health
# review legitimately produces tens of MB of it. It gets its own, larger cap:
# one number for "an agent wrote this" and one for "we wrote this".
MAX_RECORDING_BYTES = 64 * 1024 * 1024
# Aggregate bounds for one run's archive. Reached only by an evidence tree that
# has stopped being evidence.
MAX_ARCHIVE_BYTES = 96 * 1024 * 1024
MAX_ARCHIVE_FILES = 200
# Discovery bounds, which apply BEFORE anything is copied. Without them the
# copy budget bounds only the bytes that land, not the traversal that finds
# them — and a tree of a million empty files or dangling links would exhaust the
# engine before the copy budget refused anything.
MAX_SCAN_ENTRIES = 5_000
MAX_SCAN_DIRECTORIES = 250
MAX_SCAN_DEPTH = 8
# How many runs' archives are kept. The history ROWS are unbounded (they are
# tiny and their whole value is that nothing deletes them); the artifact bytes
# are not, so the newest N runs keep their evidence and older rows keep their
# verdict without a drill-down.
ARCHIVE_RETENTION = 50

_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ArchiveLimits:
    """The bounds one archive operates under.

    Injected rather than read from module constants so a test can prove the
    bounds are enforced with three small files instead of 96 MB of them — and so
    an operator-facing knob, if one is ever wanted, has an obvious home.
    """

    # Cap for an AGENT-authored artifact: the same 2 MiB the decision loader
    # applies before it will parse one.
    artifact_bytes: int = MAX_ARTIFACT_BYTES
    # Cap for the orchestrator-written PTY capture, which is legitimately larger.
    recording_bytes: int = MAX_RECORDING_BYTES
    total_bytes: int = MAX_ARCHIVE_BYTES
    files: int = MAX_ARCHIVE_FILES
    # Discovery bounds, spent while walking rather than while copying.
    scan_entries: int = MAX_SCAN_ENTRIES
    scan_directories: int = MAX_SCAN_DIRECTORIES
    scan_depth: int = MAX_SCAN_DEPTH
    retention: int = ARCHIVE_RETENTION

    def cap_for(self, member: "_PreservedMember") -> int:
        return self.recording_bytes if member.is_recording else self.artifact_bytes

    def copy_bounds(self) -> CopyBounds:
        """The subset the contained-copy owner enforces on its walk."""
        return CopyBounds(
            files=self.files,
            total_bytes=self.total_bytes,
            entries=self.scan_entries,
            directories=self.scan_directories,
            depth=self.scan_depth,
        )


class _PreservedMember:
    """One run-relative member of the preserved set, and which cap it takes."""

    def __init__(
        self, name: str, *, recursive: bool, is_recording: bool = False
    ) -> None:
        self.name = name
        self.recursive = recursive
        self.is_recording = is_recording


# What is copied out of a run directory, as run-relative members. The three
# drill-down KINDS live in here, and so does the context that makes them
# readable: the manifest a run-scoped reader resolves against, the prompt the
# run was launched with, and the whole evidence directory (evidence map, board
# snapshot, proposals) the report cites. Preserving the citations alongside the
# report is the difference between evidence and an assertion.
_PRESERVED_MEMBERS: tuple[_PreservedMember, ...] = (
    _PreservedMember("manifest.json", recursive=False),
    _PreservedMember(TERMINAL_RECORDING_FILENAME, recursive=False, is_recording=True),
    _PreservedMember("session-prompt.txt", recursive=False),
    _PreservedMember(TECH_LEAD_DATA_DIRNAME, recursive=True),
)


class FileSystemTechLeadRunArtifactArchive:
    """Preserves run artifacts under a directory this engine owns."""

    def __init__(self, root: Path, *, limits: Optional[ArchiveLimits] = None) -> None:
        self._root = root
        self._limits = limits if limits is not None else ArchiveLimits()
        self._retention = max(1, self._limits.retention)

    @classmethod
    def for_repo(cls, repo_root: Path) -> "FileSystemTechLeadRunArtifactArchive":
        """Archive handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ``TechLeadRunArtifactArchive`` port instead.
        """
        return cls(state_dir(repo_root) / ARCHIVE_DIRNAME)

    def preserve(
        self, *, run: TechLeadRunSource
    ) -> Optional[TechLeadRunArtifacts]:
        """Copy one run's inspectable artifacts into the archive.

        Staged then swapped: a failure at any point leaves whatever was already
        preserved for this run untouched, because the live destination is not
        opened until a complete copy exists beside it.

        Best-effort by contract: every failure is logged and reported as "no
        artifacts preserved", because a lost receipt must never fail the run that
        earned it.
        """
        name = _archive_name(run.run_id, run.session_name)
        destination = self._root / name
        staging = self._root / f"{_STAGING_PREFIX}{name}.{os.getpid()}"
        label = f"{run.run_id}/{run.session_name}"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self.reconcile()
            _discard(staging)
            staging.mkdir(parents=True)
            copied = self._copy_admitted(run, staging, label)
            kinds = _preserved_kinds(staging)
            if not kinds:
                _discard(staging)
                logger.info(
                    "[TECH_LEAD_RUN] Run %s preserved %d support file(s) and no"
                    " inspectable artifact; keeping no archive",
                    label,
                    copied,
                )
                return None
            _publish(staging, destination)
        except OSError:
            _discard(staging)
            logger.warning(
                "[TECH_LEAD_RUN] Could not preserve the artifacts of %s from %s",
                label,
                run.run_dir,
                exc_info=True,
            )
            return None
        logger.info(
            "[TECH_LEAD_RUN] Preserved %s for %s at %s",
            ", ".join(kind.value for kind in kinds),
            label,
            destination,
        )
        return TechLeadRunArtifacts(location=destination.resolve(), kinds=kinds)

    def reconcile(self) -> None:
        """Repair scratch state a crash left behind.

        Two cases, and neither may touch a live engine's work:

        * ``.retired-<name>`` with no live ``<name>`` — a publish was interrupted
          between renaming the old receipt aside and swapping the new one in. The
          record still points at ``<name>``, so the retired receipt is RESTORED
          rather than deleted. With a live ``<name>`` it is simply leftover.
        * ``.incoming-<name>.<pid>`` — an interrupted stage. Reclaimed only when
          that PID is gone: deleting a live engine's active stage would corrupt a
          preservation that is still running.

        Never raises: unreconciled scratch is a bigger archive, not a failed run.
        """
        for path in self._scratch(_RETIRED_PREFIX):
            live = self._root / path.name[len(_RETIRED_PREFIX) :]
            if live.exists():
                _discard(path)
                continue
            try:
                path.rename(live)
            except OSError:
                logger.warning(
                    "[TECH_LEAD_RUN] Could not restore the interrupted archive"
                    " %s to %s",
                    path,
                    live,
                    exc_info=True,
                )
                continue
            logger.info(
                "[TECH_LEAD_RUN] Restored %s from an interrupted publication",
                live,
            )
        for path in self._scratch(_STAGING_PREFIX):
            owner = _staging_owner_pid(path.name)
            if owner is None or owner == os.getpid() or process_is_alive(owner):
                continue
            if _discard(path):
                logger.info(
                    "[TECH_LEAD_RUN] Reclaimed abandoned archive scratch %s"
                    " (owner pid %d is gone)",
                    path,
                    owner,
                )

    def prune(self) -> tuple[Path, ...]:
        """Drop all but the newest ``retention`` archives; report what went.

        The caller retires the matching record locators, so a pruned run's row
        stops advertising a drill-down instead of pointing at a deleted
        directory. Reconciles first, so an interrupted publication is restored
        and counted rather than silently accumulating outside retention. Never
        raises.
        """
        try:
            self.reconcile()
            preserved = sorted(
                (path for path in self._root.iterdir() if _is_preserved_dir(path)),
                key=_newest_first,
            )
        except FileNotFoundError:
            # Nothing has been preserved yet. Not a problem, and not a warning.
            return ()
        except OSError:
            logger.warning(
                "[TECH_LEAD_RUN] Could not read the artifact archive at %s to"
                " apply retention",
                self._root,
                exc_info=True,
            )
            return ()
        removed: list[Path] = []
        for path in preserved[self._retention :]:
            resolved = path.resolve()
            if _discard(path):
                removed.append(resolved)
        if removed:
            logger.info(
                "[TECH_LEAD_RUN] Retention kept the newest %d run archive(s) and"
                " removed %d older one(s)",
                self._retention,
                len(removed),
            )
        return tuple(removed)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scratch(self, prefix: str) -> tuple[Path, ...]:
        """Scratch directories with ``prefix``, or none when unreadable."""
        try:
            return tuple(
                path
                for path in self._root.iterdir()
                if path.name.startswith(prefix) and not path.is_symlink()
            )
        except OSError:
            return ()

    def _copy_admitted(
        self, run: TechLeadRunSource, staging: Path, label: str
    ) -> int:
        """Copy every admitted member of the run directory into ``staging``.

        The anchor is reached by descending the run's own component NAMES from its
        engine-created worktree, refusing any that is not a real directory — so a
        renamed run directory with a symlink left in its place cannot redirect the
        copy at another run or out of the worktree (#6858 round 5 F16). The walk
        below it is delegated to :mod:`.contained_artifact_copy`, which owns the
        descriptor discipline and the traversal bounds; this method owns only
        WHICH members are preserved and which cap each one takes.
        """
        root_fd = open_contained_anchor(run.worktree_path, run.relative_run_parts)
        if root_fd is None:
            logger.warning(
                "[TECH_LEAD_RUN] Could not safely open the run directory %s to"
                " preserve %s",
                run.run_dir,
                label,
            )
            return 0
        budget = CopyBudget(self._limits.copy_bounds())
        copied = 0
        try:
            for member in _PRESERVED_MEMBERS:
                if budget.exhausted:
                    break
                cap = self._limits.cap_for(member)
                if member.recursive:
                    copied += copy_contained_tree(
                        root_fd, member.name, staging, cap=cap, budget=budget,
                        label=label,
                    )
                    continue
                copied += copy_contained_file(
                    root_fd, member.name, staging / member.name, cap=cap,
                    budget=budget,
                )
        finally:
            close_fd(root_fd)
        if budget.exhausted:
            logger.warning(
                "[TECH_LEAD_RUN] Stopped preserving %s after %d file(s): %s",
                label,
                copied,
                budget.exhausted_by,
            )
        return copied


def _preserved_kinds(location: Path) -> tuple[TechLeadRunArtifactKind, ...]:
    """The inspectable kinds that actually landed in ``location``."""
    return tuple(
        kind
        for kind in ARTIFACT_KIND_ORDER
        if _is_readable_file(location / Path(kind.member))
    )


def _is_readable_file(path: Path) -> bool:
    """True for a real, non-symlink, non-empty file."""
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _publish(staging: Path, destination: Path) -> None:
    """Swap a complete staged archive into place.

    The previous archive is renamed aside FIRST and deleted only once the new one
    is live, so an interrupted publish leaves either the old complete receipt or
    the new one — never a half-copied directory the record already points at. A
    crash between the two renames is repaired by :meth:`reconcile`, which finds
    the retired receipt with no live version and restores it.
    """
    retired = destination.parent / f"{_RETIRED_PREFIX}{destination.name}"
    _discard(retired)
    if destination.exists():
        destination.rename(retired)
    try:
        staging.rename(destination)
    except OSError:
        if retired.exists():
            retired.rename(destination)
        raise
    _discard(retired)


def _discard(path: Path) -> bool:
    """Remove ``path`` if it is there. True when nothing is left behind."""
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True
    except NotADirectoryError:
        unlink(path)
        return not path.exists()
    except OSError:
        logger.warning(
            "[TECH_LEAD_RUN] Could not remove %s from the artifact archive",
            path,
            exc_info=True,
        )
        return False


def _is_preserved_dir(path: Path) -> bool:
    """True for a published run archive — never staging or retirement scratch."""
    if path.name.startswith((_STAGING_PREFIX, _RETIRED_PREFIX)):
        return False
    return path.is_dir() and not path.is_symlink()


def _staging_owner_pid(name: str) -> Optional[int]:
    """The PID that owns a staging directory, or ``None`` when unnamed.

    An unowned name is left alone: it was written by a build that did not stamp
    ownership, and guessing that nothing is using it is how a live preservation
    gets deleted underneath itself.
    """
    suffix = name.rpartition(".")[2]
    return int(suffix) if suffix.isdigit() else None


def _newest_first(path: Path) -> tuple[float, str]:
    """Sort key placing the most recently published archive first."""
    try:
        return (-path.stat().st_mtime, path.name)
    except OSError:  # pragma: no cover - raced deletion
        return (0.0, path.name)


def _archive_name(run_id: str, session_name: str) -> str:
    """A filesystem-safe directory name for one session run.

    Both halves are orchestrator-minted, never agent-authored, but they are
    sanitised anyway: this builds a path, and a component that could contain a
    separator is a traversal waiting for the first caller who forgets.
    """
    return f"{_safe_component(run_id)}__{_safe_component(session_name)}"


def _safe_component(value: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in value.strip()
    )
    return safe.strip("-") or "unnamed"


__all__ = [
    "ARCHIVE_DIRNAME",
    "ARCHIVE_RETENTION",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_FILES",
    "MAX_ARTIFACT_BYTES",
    "MAX_RECORDING_BYTES",
    "MAX_SCAN_DEPTH",
    "MAX_SCAN_DIRECTORIES",
    "MAX_SCAN_ENTRIES",
    "ArchiveLimits",
    "FileSystemTechLeadRunArtifactArchive",
]
