"""Shared atomic-write toolkit for run-scoped artifacts.

Multiple writers (the persistent-session review-exchange runner, the
orchestrator-startup tempfile sweep, the legacy synchronous publisher)
need to write artifacts atomically so concurrent readers — most
notably the main orchestrator tick polling ``summary.json`` — never
observe a partially written file. Centralizing the helper here avoids
duplicating the temp-file pattern across modules and gives the sweep
one canonical prefix/suffix to scan for.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


ATOMIC_WRITE_TMP_PREFIX = "."
ATOMIC_WRITE_TMP_SUFFIX = ".tmp"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so mid-write polls never see a torn file.

    Writes to a sibling temp file on the same filesystem and renames —
    POSIX ``os.replace`` is atomic, so any reader sees either the
    pre-write content or the full new content.

    Orphaned tempfiles from a hard ``kill -9`` between ``mkstemp`` and
    ``os.replace`` are cleaned up by orchestrator-startup sweeps that
    scan for ``ATOMIC_WRITE_TMP_PREFIX``/``ATOMIC_WRITE_TMP_SUFFIX``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f"{ATOMIC_WRITE_TMP_PREFIX}{path.name}.",
        suffix=ATOMIC_WRITE_TMP_SUFFIX,
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(encoded)
        os.replace(tmp_path_str, path)
    except Exception:
        # Only clean up on failure; the success path already renamed the
        # tempfile out of existence.
        try:
            os.unlink(tmp_path_str)
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes atomically so mid-write readers never see torn content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f"{ATOMIC_WRITE_TMP_PREFIX}{path.name}.",
        suffix=ATOMIC_WRITE_TMP_SUFFIX,
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path_str, path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except FileNotFoundError:
            pass
        raise


def atomic_create_bytes(path: Path, payload: bytes) -> None:
    """Publish a complete file atomically without replacing an existing target.

    The payload is written, flushed, and closed in a sibling temporary file
    before an exclusive hard-link publishes it at ``path``. ``os.link`` fails
    with ``FileExistsError`` when another writer wins the create race, while a
    write or close failure leaves the final path absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f"{ATOMIC_WRITE_TMP_PREFIX}{path.name}.",
        suffix=ATOMIC_WRITE_TMP_SUFFIX,
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp_path_str, path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            # Preserve the publication error. Startup cleanup owns any
            # otherwise-unremovable sibling temporary file.
            pass
        raise
    else:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            # The final path is already complete and published. Do not turn
            # best-effort temporary-file cleanup into a false write failure.
            pass
