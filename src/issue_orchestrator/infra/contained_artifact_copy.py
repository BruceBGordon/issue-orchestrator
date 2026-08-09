"""Bounded, symlink-safe copying of agent-authored artifact trees (#6858 F8/F9).

:mod:`..control.validation_record_containment` does this for ONE agent-supplied
file: never reopen by pathname, walk each component with ``O_NOFOLLOW``, check the
final descriptor with ``fstat``, and stream from that descriptor. This module is
the same discipline for a TREE, which the tech-lead run archive needs because a
run's evidence directory is agent-authored and its destination is the operator's
durable state volume.

Two properties, each of which was a real hole before it was written down:

* **Race-free admission.** Every component is opened relative to its parent's
  descriptor with ``O_NOFOLLOW``, and bytes are streamed from the descriptor that
  was validated. A descendant that swaps a file — or an ancestor directory — for a
  symlink between the scan and the copy therefore loses: the open trips ``ELOOP``,
  and nothing outside the anchored root can be read. The byte ceiling is enforced
  on the bytes READ, so a file another process is appending to cannot spend more
  budget than it was admitted for.
* **Bounded discovery.** The walk is iterative (a pathological depth costs a
  refused branch, not a ``RecursionError`` through a never-raise contract), lazy
  (an unbounded directory is never materialised into a list), and capped on
  entries visited, directories entered, and depth. A per-entry failure refuses
  that entry alone: one unreadable child must never cost the artifacts already
  admitted.

Everything here reports refusals and returns counts. Nothing raises: the callers
are best-effort receipt writers, not control flow.
"""

from __future__ import annotations

import errno
import logging
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class CopyBounds:
    """Everything one copy pass may spend, on traversal as well as on bytes.

    Discovery bounds matter separately from copy bounds: without them a file
    budget limits only what LANDS, and a tree of a million empty files or
    dangling links exhausts the process before anything is refused.
    """

    files: int
    total_bytes: int
    entries: int
    directories: int
    depth: int


class CopyBudget:
    """One pass's remaining allowance, and why it stopped.

    Asked in one place so the stop condition cannot drift between the walk and
    the copy, and so the reason is reportable rather than inferred from a count.
    """

    def __init__(self, bounds: CopyBounds) -> None:
        self._bounds = bounds
        self._bytes_spent = 0
        self._files_spent = 0
        self._entries_visited = 0
        self._directories_entered = 0
        self.exhausted_by = ""

    @property
    def exhausted(self) -> bool:
        return bool(self.exhausted_by)

    def visit_entry(self) -> bool:
        """Account for one directory entry LOOKED AT, copied or not."""
        self._entries_visited += 1
        if self._entries_visited > self._bounds.entries:
            return self._exhaust(f"scan cap {self._bounds.entries} entries")
        return True

    def enter_directory(self) -> bool:
        self._directories_entered += 1
        if self._directories_entered > self._bounds.directories:
            return self._exhaust(f"scan cap {self._bounds.directories} directories")
        return True

    def admits_depth(self, depth: int) -> bool:
        """Depth is REFUSED, not exhausting: a deep branch is not a broken walk."""
        return depth <= self._bounds.depth

    def admits(self, size: int) -> bool:
        if self._files_spent + 1 > self._bounds.files:
            return self._exhaust(f"file count cap {self._bounds.files}")
        if self._bytes_spent + size > self._bounds.total_bytes:
            return self._exhaust(
                f"aggregate size cap {self._bounds.total_bytes} bytes"
            )
        return True

    def spend(self, size: int) -> None:
        self._bytes_spent += size
        self._files_spent += 1

    def _exhaust(self, reason: str) -> bool:
        if not self.exhausted_by:
            self.exhausted_by = reason
        return False


def open_anchor(root: Path) -> Optional[int]:
    """Open ``root`` as the descriptor the whole walk is anchored on."""
    try:
        return os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        logger.warning(
            "[ARTIFACT_COPY] Could not open %s to copy from it", root, exc_info=True
        )
        return None


def close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:  # pragma: no cover - already closed
        pass


def copy_contained_file(
    parent_fd: int, name: str, target: Path, *, cap: int, budget: CopyBudget
) -> int:
    """Copy one file opened relative to ``parent_fd``. Returns 1 when it landed.

    The file is opened ``O_NOFOLLOW``, validated through ``fstat`` on that
    descriptor, and streamed FROM the descriptor — the pathname is never reopened,
    so nothing that changes on disk between admission and copy can redirect the
    read.
    """
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
    except OSError as exc:
        _log_refused_open(name, exc)
        return 0
    try:
        size = _admissible_size(fd, name, cap)
        if size is None or not budget.admits(size):
            return 0
        written = _stream_capped(fd, target, cap)
        if written is None:
            return 0
        budget.spend(written)
        return 1
    finally:
        close_fd(fd)


def copy_contained_tree(
    parent_fd: int,
    name: str,
    destination: Path,
    *,
    cap: int,
    budget: CopyBudget,
    label: str,
) -> int:
    """Copy the tree at ``name`` into ``destination``, preserving its layout.

    Returns how many files landed. ``destination`` receives ``name`` as its own
    top-level directory, so the copy mirrors the source's run-relative shape.
    """
    root = open_contained_directory(parent_fd, name)
    if root is None:
        return 0
    copied = 0
    pending: list[tuple[int, Path, int]] = [(root, Path(name), 1)]
    try:
        while pending and not budget.exhausted:
            dir_fd, relative, depth = pending.pop()
            copied += _copy_directory(
                dir_fd,
                relative,
                depth,
                destination,
                cap=cap,
                budget=budget,
                label=label,
                pending=pending,
            )
    finally:
        for open_fd, _relative, _depth in pending:
            close_fd(open_fd)
        close_fd(root)
    return copied


def open_contained_directory(parent_fd: int, name: str) -> Optional[int]:
    """Open a child directory ``O_NOFOLLOW``, or ``None`` when it is not one."""
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError:
        return None


def _copy_directory(
    dir_fd: int,
    relative: Path,
    depth: int,
    destination: Path,
    *,
    cap: int,
    budget: CopyBudget,
    label: str,
    pending: list[tuple[int, Path, int]],
) -> int:
    """Copy one directory's files and queue its admissible subdirectories."""
    copied = 0
    for entry_name, is_directory in _scan(dir_fd, relative, budget):
        if budget.exhausted:
            break
        if is_directory:
            _queue_child(
                dir_fd, relative, entry_name, depth, budget, label, pending
            )
            continue
        copied += copy_contained_file(
            dir_fd,
            entry_name,
            destination / relative / entry_name,
            cap=cap,
            budget=budget,
        )
    return copied


def _queue_child(
    dir_fd: int,
    relative: Path,
    entry_name: str,
    depth: int,
    budget: CopyBudget,
    label: str,
    pending: list[tuple[int, Path, int]],
) -> None:
    """Queue a subdirectory for the walk, if depth and the budget allow it."""
    if not budget.admits_depth(depth + 1):
        logger.warning(
            "[ARTIFACT_COPY] Not descending %s while copying %s: deeper than the"
            " scan depth limit",
            relative / entry_name,
            label,
        )
        return
    if not budget.enter_directory():
        return
    child = open_contained_directory(dir_fd, entry_name)
    if child is not None:
        pending.append((child, relative / entry_name, depth + 1))


def _scan(
    dir_fd: int, relative: Path, budget: CopyBudget
) -> Iterator[tuple[str, bool]]:
    """Lazily yield ``(name, is_directory)`` for one directory's entries.

    An unreadable directory yields nothing rather than aborting the walk: one
    refused child must not cost the artifacts already admitted. Classification is
    only a HINT — the authoritative check is the ``O_NOFOLLOW`` open that follows.
    """
    try:
        scandir = os.scandir(dir_fd)
    except OSError:
        logger.warning(
            "[ARTIFACT_COPY] Skipping unreadable directory %s", relative, exc_info=True
        )
        return
    try:
        for entry in scandir:
            if not budget.visit_entry():
                return
            try:
                if entry.is_symlink():
                    logger.warning(
                        "[ARTIFACT_COPY] Refusing %s: symlinks are not artifacts,"
                        " and following one would copy from outside the source",
                        relative / entry.name,
                    )
                    continue
                yield (entry.name, entry.is_dir(follow_symlinks=False))
            except OSError:
                # A vanished or unstattable entry costs itself, nothing more.
                continue
    finally:
        scandir.close()


def _log_refused_open(name: str, exc: OSError) -> None:
    """Explain a refused open.

    A missing member is the normal case — most runs write no ``session-prompt`` —
    so it is silent. ``ELOOP`` is a symlink that appeared between the scan and the
    open, which is exactly the race this walk exists to lose safely.
    """
    if exc.errno == errno.ENOENT:
        return
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        logger.warning("[ARTIFACT_COPY] Refusing %s: it is a symlink (%s)", name, exc)
        return
    logger.debug("[ARTIFACT_COPY] Not copying %s: %s", name, exc)


def _admissible_size(fd: int, name: str, cap: int) -> Optional[int]:
    """The size behind ``fd``, or ``None`` when the file must not be copied.

    Empty is treated as absent throughout the run-artifact surfaces: an empty
    recording or a zero-byte decision is a capture gap, and offering a drill-down
    into one only teaches an operator to distrust the buttons.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    if not stat_module.S_ISREG(st.st_mode):
        logger.warning("[ARTIFACT_COPY] Refusing %s: not a regular file", name)
        return None
    if st.st_size <= 0:
        return None
    if st.st_size > cap:
        logger.warning(
            "[ARTIFACT_COPY] Refusing %s: %d bytes exceeds the %d byte cap",
            name,
            st.st_size,
            cap,
        )
        return None
    return st.st_size


def _stream_capped(fd: int, target: Path, cap: int) -> Optional[int]:
    """Stream at most ``cap`` bytes from ``fd`` into ``target``.

    The ceiling is enforced on the BYTES READ, not on the earlier ``fstat``: a
    file another process is still appending to would otherwise be admitted at one
    size and copied at another, spending budget it was never granted. Over the
    cap, the partial target is removed and the artifact is refused.
    """
    written = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A dup so ``fdopen`` can own its handle while the caller keeps owning the
        # descriptor it validated.
        with os.fdopen(os.dup(fd), "rb", closefd=True) as source:
            with open(target, "wb") as sink:
                while True:
                    chunk = source.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > cap:
                        raise _ArtifactTooLarge(target, cap)
                    sink.write(chunk)
    except _ArtifactTooLarge as exc:
        logger.warning(
            "[ARTIFACT_COPY] Refusing %s: it grew past the %d byte cap while being"
            " copied",
            exc.target.name,
            exc.cap,
        )
        unlink(target)
        return None
    except OSError:
        logger.warning(
            "[ARTIFACT_COPY] Could not copy %s", target.name, exc_info=True
        )
        unlink(target)
        return None
    return written


class _ArtifactTooLarge(Exception):
    """Raised internally when a stream exceeds its cap mid-copy."""

    def __init__(self, target: Path, cap: int) -> None:
        super().__init__(f"{target} exceeded {cap} bytes")
        self.target = target
        self.cap = cap


def unlink(target: Path) -> None:
    """Remove a file if it is there, never raising."""
    try:
        target.unlink()
    except OSError:
        pass


__all__ = [
    "CopyBounds",
    "CopyBudget",
    "close_fd",
    "copy_contained_file",
    "copy_contained_tree",
    "open_anchor",
    "open_contained_directory",
    "unlink",
]
