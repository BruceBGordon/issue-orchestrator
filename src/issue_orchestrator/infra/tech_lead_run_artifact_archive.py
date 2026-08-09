"""Engine-owned durable home for finished tech-lead run artifacts (#6858 F4/F6).

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

Three things make this a BOUNDED owner rather than a copy loop (#6858 round 2
F6/A3), because the source is agent-authored and the destination is the
operator's state volume:

* **Admission.** Only regular files are admitted, never symlinks or devices, and
  never a path that resolves outside the run directory. ``shutil.copytree``
  follows links by default, which would let a run's evidence tree pull arbitrary
  files into a durable engine-owned directory.
* **Bounds.** A per-member size cap (the same 2 MiB the decision loader applies
  before parsing an agent artifact), an aggregate byte cap, and a file-count cap.
  Everything dropped is logged: a silent cap reads as "we preserved it all".
* **Atomic publication + retention.** The copy is staged in a sibling directory
  and swapped in only once it is complete, so a failed retry cannot destroy the
  receipt that was already there. Retention keeps the newest
  ``ARCHIVE_RETENTION`` runs and REPORTS what it removed, so the caller can
  retire the matching record locators in the same breath.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ..domain.tech_lead_run_artifacts import (
    ARTIFACT_KIND_ORDER,
    TECH_LEAD_DATA_DIRNAME,
    TERMINAL_RECORDING_FILENAME,
    TechLeadRunArtifactKind,
    TechLeadRunArtifacts,
)
from .repo_identity import state_dir

logger = logging.getLogger(__name__)

# The archive's directory name under the state directory.
ARCHIVE_DIRNAME = "tech-lead-runs"

# Staging and retirement prefixes. Dot-prefixed so a half-written or
# being-deleted directory is never mistaken for a preserved run by retention or
# by anything listing the archive.
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
# How many runs' archives are kept. The history ROWS are unbounded (they are
# tiny and their whole value is that nothing deletes them); the artifact bytes
# are not, so the newest N runs keep their evidence and older rows keep their
# verdict without a drill-down.
ARCHIVE_RETENTION = 50


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
    retention: int = ARCHIVE_RETENTION

    def cap_for(self, member: "_PreservedMember") -> int:
        return self.recording_bytes if member.is_recording else self.artifact_bytes


class _ArchiveBudget:
    """The aggregate bounds one preservation may spend.

    A tiny object rather than two counters threaded through the walk, so the
    stop condition is asked in one place and the reason it stopped is reportable.
    """

    def __init__(self, *, max_bytes: int, max_files: int) -> None:
        self._max_bytes = max_bytes
        self._max_files = max_files
        self.bytes_spent = 0
        self.files_spent = 0
        self.exhausted_by = ""

    def admits(self, size: int) -> bool:
        if self.files_spent + 1 > self._max_files:
            self.exhausted_by = f"file count cap {self._max_files}"
            return False
        if self.bytes_spent + size > self._max_bytes:
            self.exhausted_by = f"aggregate size cap {self._max_bytes} bytes"
            return False
        return True

    def spend(self, size: int) -> None:
        self.bytes_spent += size
        self.files_spent += 1


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
        self,
        *,
        run_id: str,
        session_name: str,
        run_dir: Path,
    ) -> Optional[TechLeadRunArtifacts]:
        """Copy one run's inspectable artifacts into the archive.

        Staged then swapped: a failure at any point leaves whatever was already
        preserved for this run untouched, because the live destination is not
        opened until a complete copy exists beside it.

        Best-effort by contract: every filesystem failure is logged and reported
        as "no artifacts preserved", because a lost receipt must never fail the
        run that earned it.
        """
        name = _archive_name(run_id, session_name)
        destination = self._root / name
        staging = self._root / f"{_STAGING_PREFIX}{name}"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            _discard(staging)
            staging.mkdir(parents=True)
            copied = self._copy_admitted(run_dir, staging, run_id, session_name)
            kinds = _preserved_kinds(staging)
            if not kinds:
                _discard(staging)
                logger.info(
                    "[TECH_LEAD_RUN] Run %s/%s preserved %d support file(s) and no"
                    " inspectable artifact; keeping no archive",
                    run_id,
                    session_name,
                    copied,
                )
                return None
            _publish(staging, destination)
        except OSError:
            _discard(staging)
            logger.warning(
                "[TECH_LEAD_RUN] Could not preserve the artifacts of %s/%s from %s",
                run_id,
                session_name,
                run_dir,
                exc_info=True,
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

    def prune(self) -> tuple[Path, ...]:
        """Drop all but the newest ``retention`` archives; report what went.

        The caller retires the matching record locators, so a pruned run's row
        stops advertising a drill-down instead of pointing at a deleted
        directory. Never raises: a retention pass that cannot run is a bigger
        archive, not a failed run.
        """
        try:
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

    def _copy_admitted(
        self, run_dir: Path, staging: Path, run_id: str, session_name: str
    ) -> int:
        """Copy every admitted member of ``run_dir`` into ``staging``.

        Returns how many files landed. Refusals and bound exhaustion are logged
        rather than raised: one hostile or oversized file must not cost the run
        the rest of its receipt.
        """
        run_root = run_dir.resolve()
        budget = _ArchiveBudget(
            max_bytes=self._limits.total_bytes, max_files=self._limits.files
        )
        copied = 0
        for member in _PRESERVED_MEMBERS:
            for source, relative in member.sources(run_dir):
                size = _admissible_size(
                    source, run_root, self._limits.cap_for(member)
                )
                if size is None:
                    continue
                if not budget.admits(size):
                    logger.warning(
                        "[TECH_LEAD_RUN] Stopped preserving %s/%s at %s: %s",
                        run_id,
                        session_name,
                        relative,
                        budget.exhausted_by,
                    )
                    return copied
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                budget.spend(size)
                copied += 1
        return copied


class _PreservedMember:
    """One run-relative member of the preserved set, and which cap it takes."""

    def __init__(self, name: str, *, recursive: bool, is_recording: bool = False) -> None:
        self._name = name
        self._recursive = recursive
        self.is_recording = is_recording

    def sources(self, run_dir: Path) -> Iterator[tuple[Path, Path]]:
        """``(source, run-relative target)`` for each candidate file.

        Directory members are walked WITHOUT following links, and only real
        subdirectories are descended: a symlinked directory inside an
        agent-authored evidence tree is not a directory this archive walks.
        """
        root = run_dir / self._name
        if not self._recursive:
            yield (root, Path(self._name))
            return
        if root.is_symlink() or not root.is_dir():
            return
        for source in sorted(_iter_regular_files(root)):
            yield (source, Path(self._name) / source.relative_to(root))


def _iter_regular_files(root: Path) -> Iterator[Path]:
    """Every non-symlink file under ``root``, depth-first, links never followed."""
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _iter_regular_files(entry)
        elif entry.is_file():
            yield entry


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


def _admissible_size(source: Path, run_root: Path, max_bytes: int) -> Optional[int]:
    """The file's size, or ``None`` when it must not be preserved.

    Refuses anything that is not a real, non-empty, contained regular file.
    Empty is treated as absent throughout the run-artifact surfaces: an empty
    recording or a zero-byte decision is a capture gap, and offering a
    drill-down into one only teaches an operator to distrust the buttons.
    """
    try:
        if source.is_symlink():
            logger.warning(
                "[TECH_LEAD_RUN] Refusing to preserve %s: symlinks are not"
                " artifacts, and following one would copy from outside the run",
                source,
            )
            return None
        if not source.is_file():
            return None
        # Containment: raises ValueError when the real file lives outside the run
        # directory, which is the only way a non-symlink candidate can escape.
        source.resolve().relative_to(run_root)
        size = source.stat().st_size
    except ValueError:
        logger.warning(
            "[TECH_LEAD_RUN] Refusing to preserve %s: it resolves outside the run"
            " directory",
            source,
        )
        return None
    except OSError:
        return None
    if size <= 0:
        return None
    if size > max_bytes:
        logger.warning(
            "[TECH_LEAD_RUN] Refusing to preserve %s: %d bytes exceeds the %d"
            " byte cap for this artifact",
            source,
            size,
            max_bytes,
        )
        return None
    return size


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
    the new one — never a half-copied directory the record already points at.
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
        char if char.isalnum() or char in "-_." else "-" for char in value.strip()
    )
    return safe.strip(".-") or "unnamed"


__all__ = [
    "ARCHIVE_DIRNAME",
    "ArchiveLimits",
    "ARCHIVE_RETENTION",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_FILES",
    "MAX_ARTIFACT_BYTES",
    "MAX_RECORDING_BYTES",
    "FileSystemTechLeadRunArtifactArchive",
]
